"""Bounded capability proof for attributable retained application tasks."""

import base64
import inspect
import json
from pathlib import Path
from collections.abc import Callable

from control_plane import runtime_platform_credentials as credentials
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.deploy_reference import docker_image_repository, docker_image_digest
from control_plane.contracts.runtime_identity import parse_runtime_identity_payload
from control_plane.dokploy import api, source
from control_plane.generic_web_testing_exclusion import bind_testing_recipient, _target_reservations
from control_plane.provider_operations import build_provider_operation_key
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_application_host import run_application_host_program
from control_plane.workflows.generic_web_application_snapshot import (
    generic_web_provider_deployment_record_id,
)
from control_plane.workflows.generic_web_deploy_provider import generic_web_provider_target_key
from control_plane.workflows.generic_web_deploy_provider import (
    decode_generic_web_provider_reconciliation_target,
)


def retained_application_capability_spec(
    store: PostgresRecordStore, root: Path, product: str, context: str
) -> dict[str, object]:
    _profile, target, target_id, provider = bind_testing_recipient(
        store, product, context, "testing"
    )
    if target.target_type != "application":
        raise ValueError("Retained capability proof requires an application.")
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="application", target_id=target_id.target_id
    )
    environment = payload.get("environment")
    project = environment.get("project") if isinstance(environment, dict) else None
    if not isinstance(environment, dict) or not isinstance(project, dict):
        raise ValueError("Retained parent runtime inputs are unavailable.")
    for item in (payload, environment, project):
        if "env" not in item or item["env"] is not None and not isinstance(item["env"], str):
            raise ValueError("Retained inherited runtime inputs are unavailable.")
        credentials.refuse_platform_credentials(
            api.parse_dokploy_env_text(str(item.get("env") or "")),
            target="retained testing application",
            source="the retained provider runtime",
        )
    mounts = payload.get("mounts")
    if not isinstance(mounts, list):
        raise ValueError("Retained mount inputs are unavailable.")
    for mount in mounts:
        if not isinstance(mount, dict):
            raise ValueError("Retained mount inputs are incomplete.")
        if (
            mount.get("type") == "bind"
            and str(mount.get("hostPath", "")).replace("/var/run/", "/run/") == "/run/docker.sock"
        ):
            raise ValueError("A retained daemon socket can reach replacement resources.")
    key = generic_web_provider_target_key(
        provider_id=provider.provider_id,
        context=context,
        instance="testing",
        target_type="application",
        target_id=target_id.target_id,
    )
    from control_plane.drivers.generic_web_dispatch import GenericWebDeployEnvelope

    deployments = {}
    for row in _target_reservations(
        store, product=product, context=context, target_key=key, target_id=target_id.target_id
    ):
        if row.provider_target_key != key:
            continue
        captured = decode_generic_web_provider_reconciliation_target(row.reconciliation_key)
        if captured.original_event_deploy is None:
            continue
        request = GenericWebDeployEnvelope.model_validate(captured.original_event_deploy)
        record_id = generic_web_provider_deployment_record_id(
            build_provider_operation_key(
                scope=row.scope,
                route_path=row.route_path,
                idempotency_key=row.idempotency_key,
                request_fingerprint=row.request_fingerprint,
                reconciliation_key=row.reconciliation_key,
            ),
            context=context,
            instance="testing",
        )
        if request.product != product or request.deploy.instance != "testing":
            raise ValueError("The retained event belongs to another lane.")
        deployments[record_id] = {
            "artifact": request.deploy.artifact_id,
            "provider_reference": request.deploy.deploy_reference or request.deploy.artifact_id,
            "source": request.deploy.source_git_ref,
        }
    try:
        inventory = store.read_environment_inventory(context_name=context, instance_name="testing")
        baseline = store.read_deployment_record(inventory.deployment_record_id)
    except FileNotFoundError:
        pass
    else:
        identity = parse_runtime_identity_payload(baseline.runtime_identity)
        if (
            baseline.deployed_target is not None
            and baseline.deployed_target.target_id == target_id.target_id
            and identity is not None
            and (identity.product, identity.context, identity.instance)
            == (product, context, "testing")
            and inventory.artifact_identity == baseline.artifact_identity
            and inventory.source_git_ref == baseline.source_git_ref
        ):
            deployments[baseline.record_id] = {
                "artifact": identity.artifact_id,
                "provider_reference": identity.image_reference,
                "source": identity.source_git_ref,
            }
    app_name = payload.get("appName")
    if not isinstance(app_name, str) or not app_name or not deployments:
        raise ValueError("The retained original has no attributable application identity.")
    return {
        "product": product,
        "context": context,
        "application": app_name,
        "application_id": target_id.target_id,
        "deployments": deployments,
    }


def runtime_capability_program_header() -> str:
    """Fixed host programs use the same credential classifiers as ordinary delivery."""
    return (
        "import re\nfrom dataclasses import dataclass\nfrom urllib.parse import urlparse\n"
        "from typing import Literal\nfrom collections.abc import Mapping\n"
        + "PLATFORM_CREDENTIAL_KEYS="
        + repr(credentials.PLATFORM_CREDENTIAL_KEYS)
        + "\n"
        + "GITHUB_TOKEN_VALUE_PATTERN=re.compile("
        + repr(credentials.GITHUB_TOKEN_VALUE_PATTERN.pattern)
        + ")\n"
        + "PlatformCredentialReason=Literal['platform_credential_key','github_token_value']\n"
        + inspect.getsource(credentials.PlatformCredentialFinding)
        + "\n"
        + inspect.getsource(credentials.platform_credential_reason)
        + "\n"
        + inspect.getsource(credentials.find_platform_credentials)
        + "\n"
        + "_CREDENTIAL_NAME_PARTS="
        + repr(credentials._CREDENTIAL_NAME_PARTS)
        + "\n"
        + inspect.getsource(credentials.looks_like_credential)
        + "\n"
    )


def retained_application_capability_program(spec: dict[str, object]) -> str:
    header = (
        "import base64,hashlib,json,re,subprocess,sys\nfrom pathlib import Path\n"
        + runtime_capability_program_header()
        + "_SHA256_DIGEST_RE=re.compile(r'^sha256:[0-9a-f]{64}$')\n"
        + inspect.getsource(docker_image_repository)
        + "\n"
        + inspect.getsource(docker_image_digest)
        + "\n"
    )
    encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
    return (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
        + encoded
        + " <<'LP_RETAINED_CAPABILITY'\n"
        + header
        + RETAINED_APPLICATION_CAPABILITY_PROGRAM
        + "\nLP_RETAINED_CAPABILITY\n"
    )


def inspect_retained_application_capabilities(
    *,
    store: PostgresRecordStore,
    root: Path,
    product: str,
    context: str,
    namespace: str,
    before_effect: Callable[[str], None],
) -> str:
    spec = retained_application_capability_spec(store, root, product, context)
    script = retained_application_capability_program(spec)
    result = run_application_host_program(
        root=root,
        retained_target_id=str(spec["application_id"]),
        context=context,
        namespace=namespace,
        name="Launchplane retained application capabilities",
        prepare_script=script,
        readback_script=script,
        before_effect=before_effect,
    )
    if result.get("testing_resources_ok") != "true" or result.get(
        "testing_resources_binding_sha256"
    ) != canonical_json_sha256(spec):
        raise ValueError("Attributable retained application capability remains unknown.")
    return canonical_json_sha256(spec)


RETAINED_APPLICATION_CAPABILITY_PROGRAM = r"""
def docker(*arguments):
    result = subprocess.run(["docker", *arguments], check=False, capture_output=True, text=True, timeout=60)
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("inventory unavailable")
    return result.stdout

def rows(*arguments):
    values = docker(*arguments).splitlines()
    if len(values) > 10000 or len(values) != len(set(values)) or any(not item.strip() for item in values):
        raise ValueError("inventory incomplete")
    return sorted(values)

def capability(configuration):
    if not isinstance(configuration, dict) or not isinstance(configuration.get("Env"), list):
        raise ValueError("captured runtime unavailable")
    values = {}
    for value in configuration["Env"]:
        if not isinstance(value, str) or "=" not in value:
            raise ValueError("captured runtime incomplete")
        key, value = value.split("=", 1)
        if key in values:
            raise ValueError("captured runtime ambiguous")
        values[key] = value
    if find_platform_credentials(values) or values.get("DOCKER_HOST"):
        raise ValueError("retained platform authority")
    return values

def mounts(values):
    if not isinstance(values, list):
        raise ValueError("captured mounts unavailable")
    for mount in values:
        if not isinstance(mount, dict):
            raise ValueError("captured mounts incomplete")
        if str(mount.get("Type", "")).lower() == "bind":
            source = mount.get("Source")
            if not isinstance(source, str) or not source.startswith("/"):
                raise ValueError("captured bind unavailable")
            if source in {"/run/docker.sock", "/var/run/docker.sock"} or str(Path(source).resolve()) == "/run/docker.sock":
                raise ValueError("retained daemon authority")

def repository(reference):
    return docker_image_repository(reference.split("@", 1)[0])

def image_matches(reference, artifact, provider):
    if "@" in reference:
        return repository(reference) == repository(artifact) and docker_image_digest(reference) == docker_image_digest(artifact)
    return reference == provider

try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    service = json.loads(docker("service", "inspect", spec["application"]))
    if not isinstance(service, list) or len(service) != 1 or service[0].get("Spec", {}).get("Name") != spec["application"]:
        raise ValueError("retained service unavailable")
    task_ids = rows("service", "ps", "--no-trunc", "-q", spec["application"])
    container_filter = "label=com.docker.swarm.service.name=" + spec["application"]
    container_ids = rows("ps", "-aq", "--no-trunc", "--filter", container_filter)
    attributable = False
    for identifier in task_ids:
        task = json.loads(docker("inspect", "--type", "task", identifier))
        if not isinstance(task, list) or len(task) != 1 or task[0].get("ID") != identifier or task[0].get("ServiceID") != service[0]["ID"]:
            raise ValueError("retained task unavailable")
        configuration = task[0].get("Spec", {}).get("ContainerSpec")
        values = capability(configuration)
        mounts(configuration.get("Mounts", []))
        identity = json.loads(values.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "null"))
        if isinstance(identity, dict) and identity.get("deployment_record_id") in spec["deployments"]:
            if (identity.get("product"), identity.get("context"), identity.get("instance")) != (spec["product"], spec["context"], "testing"):
                raise ValueError("retained runtime belongs to another lane")
            expected = spec["deployments"][identity["deployment_record_id"]]
            artifact, provider = expected["artifact"], expected["provider_reference"]
            if (identity.get("artifact_id") != artifact or identity.get("source_git_ref") != expected["source"]
                or not docker_image_digest(artifact) or not image_matches(configuration.get("Image", ""), artifact, provider)):
                raise ValueError("retained immutable image unavailable")
            container_id = task[0].get("Status", {}).get("ContainerStatus", {}).get("ContainerID")
            if not container_id:
                raise ValueError("retained execution identity unavailable")
            if container_id in container_ids:
                container = json.loads(docker("inspect", container_id))[0]
                image_id = container.get("Image")
                if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
                    raise ValueError("retained image identity unavailable")
                image = json.loads(docker("image", "inspect", image_id))
                if (not isinstance(image, list) or len(image) != 1 or image[0].get("Id") != image_id
                    or not isinstance(image[0].get("RepoDigests"), list)
                    or not any(isinstance(value, str) and repository(value) == repository(artifact)
                        and docker_image_digest(value) == docker_image_digest(artifact) for value in image[0]["RepoDigests"])):
                    raise ValueError("retained image digest unavailable")
                if not image_matches(container.get("Config", {}).get("Image", ""), artifact, provider):
                    raise ValueError("retained configured image changed")
            elif "@" not in configuration.get("Image", ""):
                raise ValueError("removed task has no immutable image proof")
            attributable = True
    for identifier in container_ids:
        container = json.loads(docker("inspect", identifier))
        if not isinstance(container, list) or len(container) != 1 or container[0].get("Id") != identifier:
            raise ValueError("container inventory incomplete")
        configuration = container[0].get("Config")
        labels = configuration.get("Labels") if isinstance(configuration, dict) else None
        if not isinstance(labels, (dict, type(None))):
            raise ValueError("container ownership incomplete")
        if (labels or {}).get("com.docker.swarm.service.name") != spec["application"]:
            continue
        capability(configuration)
        mounts(container[0].get("Mounts"))
    if (not attributable or rows("service", "ps", "--no-trunc", "-q", spec["application"]) != task_ids
        or rows("ps", "-aq", "--no-trunc", "--filter", container_filter) != container_ids
        or json.loads(docker("service", "inspect", spec["application"])) != service):
        raise ValueError("retained attribution changed")
    print("testing_resources_binding_sha256=" + hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    print("testing_resources_ok=true")
except Exception:
    print("testing_resources_ok=false")
    sys.exit(1)
"""
