import inspect
from pathlib import Path
import re

import click

from control_plane.contracts.application_resource_observation import ApplicationResourceObservation
from control_plane.contracts.deploy_reference import (
    canonical_docker_image_repository,
    docker_image_repository,
)
from control_plane.docker_errors import docker_daemon_authorization_denied
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.generic_web_testing_exclusion import bind_testing_recipient
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_application_capabilities import single_local_swarm_node


def _target_resources(payload: api.JsonObject, application_id: str) -> dict[str, object]:
    application, server = payload.get("appName"), payload.get("serverId")
    mounts = payload.get("mounts")
    if (
        payload.get("applicationId") != application_id
        or not isinstance(application, str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", application) is None
        or not isinstance(mounts, list)
        or len(mounts) > 10000
    ):
        raise ValueError("Application resource identity is unavailable.")
    server = native_copy_stream.require_registered_remote_server(server)
    volumes: set[str] = set()
    unsupported = False
    for mount in mounts:
        if not isinstance(mount, dict):
            raise ValueError("Application mount inventory is unavailable.")
        name = mount.get("volumeName")
        if (
            mount.get("type") == "volume"
            and isinstance(name, str)
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name)
        ):
            volumes.add(name)
        else:
            unsupported = True
    return {
        "application": application,
        "application_id": application_id,
        "server_id": server,
        "configured_volumes": sorted(volumes),
        "configured_unsupported_mounts": unsupported,
    }


def collect_application_resource_observation(
    store: PostgresRecordStore, *, root: Path, product: str, context: str
) -> ApplicationResourceObservation:
    checkpoint = "recipient_binding_unavailable"
    try:
        profile, target, target_id, provider = bind_testing_recipient(
            store, product, context, "testing"
        )
        if profile.driver_id != "generic-web" or target.target_type != "application":
            return ApplicationResourceObservation(reason_code="resource_inventory_unsupported")
        checkpoint = "provider_config_unavailable"
        host, token = source.read_dokploy_config(
            control_plane_root=root, database_url=store.database_url
        )
        checkpoint = "provider_target_unavailable"
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=target_id.target_id
        )
        checkpoint = "registered_server_unavailable"
        native_copy_stream.require_registered_remote_server(payload.get("serverId"))
        checkpoint = "provider_target_unavailable"
        spec = _target_resources(payload, target_id.target_id)
        frames: list[dict[str, object]] = []
        checkpoint = "resource_inventory_unavailable"
        native_copy_stream._read_remote(
            host=host,
            token=token,
            server_id=str(spec["server_id"]),
            program=APPLICATION_RESOURCE_OBSERVATION_PROGRAM,
            payload=spec,
            receive=frames.append,
            timeout_seconds=90,
        )
        checkpoint = "provider_target_changed"
        current = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=target_id.target_id
        )
        if _target_resources(current, target_id.target_id) != spec or bind_testing_recipient(
            store, product, context, "testing"
        ) != (profile, target, target_id, provider):
            return ApplicationResourceObservation(reason_code="provider_target_changed")
        checkpoint = "resource_observation_invalid"
        if len(frames) != 1 or set(frames[0]) != {"type", "result"}:
            raise ValueError("Application resource observation is incomplete.")
        if frames[0]["type"] != "complete":
            raise ValueError("Application resource observation is incomplete.")
        return ApplicationResourceObservation.model_validate(frames[0]["result"])
    except native_copy_stream.NativeSourceAuthorizationDenied:
        raise
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise native_copy_stream.NativeSourceAuthorizationDenied(
                "The provider refused the application resource read."
            ) from None
    except (ValueError, OSError, TypeError, KeyError, click.ClickException):
        pass
    return ApplicationResourceObservation.model_validate({"reason_code": checkpoint})


APPLICATION_RESOURCE_OBSERVATION_PROGRAM = (
    "from collections.abc import Callable\nfrom typing import Any\n"
    "import base64,json,os,re,subprocess,sys,time\nfrom pathlib import Path\n"
    + inspect.getsource(docker_daemon_authorization_denied)
    + "\n"
    + inspect.getsource(docker_image_repository)
    + "\n"
    + inspect.getsource(canonical_docker_image_repository)
    + "\n"
    + inspect.getsource(single_local_swarm_node)
    + r"""

deadline = time.monotonic() + 70

class ReadDenied(Exception):
    pass

class InventoryChanged(Exception):
    pass

def docker(*arguments):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError('Observation deadline exceeded')
    result = subprocess.run(['docker', *arguments], capture_output=True, check=False, timeout=min(10, remaining))
    if result.returncode:
        if docker_daemon_authorization_denied(result.stderr):
            raise ReadDenied()
        raise ValueError('Docker metadata unavailable')
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError('Docker metadata exceeds bound')
    return result.stdout.decode('utf-8', errors='strict')

def identifiers(*arguments):
    values = docker(*arguments).splitlines()
    if (len(values) > 10000 or len(set(values)) != len(values)
        or any(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', value) is None for value in values)):
        raise ValueError('Incomplete identity inventory')
    return sorted(values)

def row(*arguments):
    result = json.loads(docker(*arguments))
    if not isinstance(result, dict):
        raise ValueError('Invalid metadata row')
    return result

SERVICE_FORMAT = '{"ID":{{json .ID}},"Version":{{json .Version.Index}},"Name":{{json .Spec.Name}},"Mode":{{json .Spec.Mode}},"Mounts":{{json .Spec.TaskTemplate.ContainerSpec.Mounts}},"Image":{{json .Spec.TaskTemplate.ContainerSpec.Image}},"Update":{{json .UpdateStatus}}}'
TASK_FORMAT = '{"ID":{{json .ID}},"ServiceID":{{json .ServiceID}},"NodeID":{{json .NodeID}},"DesiredState":{{json .DesiredState}},"Status":{{json .Status}},"Mounts":{{json .Spec.ContainerSpec.Mounts}}}'
CONTAINER_FORMAT = '{"Id":{{json .Id}},"Name":{{json .Name}},"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}},"Status":{{json .State.Status}}}'
VOLUME_FORMAT = '{"Name":{{json .Name}},"Driver":{{json .Driver}},"Options":{{json .Options}},"Mountpoint":{{json .Mountpoint}}}'
IMAGE_FORMAT = '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}},"Volumes":{{json .Config.Volumes}}}'

def observation(spec):
    application = spec['application']
    if re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', application) is None:
        raise ValueError('Invalid application identity')
    nodes = single_local_swarm_node(docker)
    service = row('service', 'inspect', '--format', SERVICE_FORMAT, application)
    service_id, version = service['ID'], service['Version']
    if (not isinstance(service_id, str) or not service_id or service['Name'] != application
        or type(version) is not int or version < 1):
        raise ValueError('Invalid service identity')
    replicas = service['Mode']['Replicated']['Replicas']
    if type(replicas) is not int or replicas < 0:
        raise ValueError('Unsupported service mode')
    tasks = identifiers('service', 'ps', '--no-trunc', '--quiet', service_id)
    containers = identifiers('ps', '-aq', '--no-trunc')
    volumes = identifiers('volume', 'ls', '--quiet')
    referenced = set(spec['configured_volumes'])
    configured_count = len(referenced)
    unsupported = spec['configured_unsupported_mounts']

    def mounts(value):
        nonlocal unsupported
        if value is None:
            return
        if not isinstance(value, list) or len(value) > 10000:
            raise ValueError('Incomplete mounts')
        for mount in value:
            if not isinstance(mount, dict):
                raise ValueError('Incomplete mount')
            kind = mount.get('Type')
            name = mount.get('Name') if 'Name' in mount else mount.get('Source')
            if kind == 'tmpfs':
                continue
            if kind == 'volume' and isinstance(name, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', name):
                referenced.add(name)
            else:
                unsupported = True

    mounts(service['Mounts'])
    terminal = True
    task_rows = {}
    task_containers = {}
    for identifier in tasks:
        task = row('inspect', '--type', 'task', '--format', TASK_FORMAT, identifier)
        if (task['ID'] != identifier or task['ServiceID'] != service_id
            or task['NodeID'] not in ('', nodes[0]['ID']) or not isinstance(task['Status'], dict)):
            raise ValueError('Task attribution unavailable')
        state = task['Status'].get('State')
        desired = task['DesiredState']
        if not isinstance(state, str) or not isinstance(desired, str):
            raise ValueError('Task state unavailable')
        container_status = task['Status'].get('ContainerStatus')
        if container_status is None:
            container_status = {}
        if not isinstance(container_status, dict):
            raise ValueError('Task container reference unavailable')
        container_id = container_status.get('ContainerID', '')
        if not isinstance(container_id, str) or container_id and re.fullmatch(r'[a-f0-9]{64}', container_id) is None:
            raise ValueError('Task container reference unavailable')
        if container_id:
            if container_id in task_containers:
                raise ValueError('Conflicting task container references')
            task_containers[container_id] = identifier
        terminal = terminal and state in {'complete','shutdown','failed','rejected','remove'} and desired in {'shutdown','remove'}
        mounts(task['Mounts'])
        task_rows[identifier] = task
    owned = 0
    container_rows = {}
    for identifier in containers:
        container = row('inspect', '--type', 'container', '--format', CONTAINER_FORMAT, identifier)
        labels = container['Labels']
        if (container['Id'] != identifier or not isinstance(container['Name'], str)
            or not isinstance(labels, (dict, type(None))) or not isinstance(container['Mounts'], list)):
            raise ValueError('Container attribution unavailable')
        labels = labels or {}
        linked_task = task_containers.get(identifier)
        if linked_task is not None and (
            labels.get('com.docker.swarm.service.id') != service_id
            or labels.get('com.docker.swarm.task.id') != linked_task
            or task_rows[linked_task]['NodeID'] != nodes[0]['ID']
        ):
            raise ValueError('Conflicting task container attribution')
        matches = (labels.get('com.docker.swarm.service.id') == service_id
                   or labels.get('com.docker.swarm.service.name') == application
                   or container['Name'].lstrip('/').startswith(application + '.'))
        if matches:
            if (labels.get('com.docker.swarm.service.id') != service_id
                or labels.get('com.docker.swarm.service.name') not in (None, application)
                or not isinstance(labels.get('com.docker.swarm.task.id'), str)
                or not labels['com.docker.swarm.task.id']
                or labels.get('com.docker.swarm.node.id', nodes[0]['ID']) != nodes[0]['ID']):
                raise ValueError('Conflicting container attribution')
            container_task = labels['com.docker.swarm.task.id']
            if container_task in task_rows:
                expected_container = (task_rows[container_task]['Status'].get('ContainerStatus') or {}).get('ContainerID')
                if expected_container and expected_container != identifier:
                    raise ValueError('Conflicting container task reference')
            owned += 1
            mounts(container['Mounts'])
        container_rows[identifier] = container
    root = Path(json.loads(docker('info', '--format', '{{json .DockerRootDir}}')))
    if not root.is_absolute() or root.is_symlink():
        raise ValueError('Unsupported Docker root')
    root = root.resolve(strict=True)
    parent = root / 'volumes'
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError('Unsupported volume root')
    empty = nonempty = unavailable = 0
    volume_rows = {}
    for name in sorted(referenced):
        if name not in volumes:
            unavailable += 1
            continue
        volume = row('volume', 'inspect', '--format', VOLUME_FORMAT, name)
        if volume['Name'] != name:
            raise ValueError('Volume identity unavailable')
        volume_rows[name] = volume
        expected = parent / name / '_data'
        if (volume['Driver'] != 'local' or volume['Options'] not in (None, {})
            or volume['Mountpoint'] != str(expected) or expected.parent.is_symlink()
            or expected.is_symlink() or not expected.is_dir()):
            unavailable += 1
            continue
        with os.scandir(expected) as entries:
            present = next(entries, None) is not None
        nonempty += int(present)
        empty += int(not present)
    image_volumes = None
    image = service['Image']
    if isinstance(image, str) and re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}', image):
        try:
            image_row = row('image', 'inspect', '--format', IMAGE_FORMAT, image)
            digests = image_row['RepoDigests']
            declared = image_row['Volumes']
            if (isinstance(digests, list)
                and any(isinstance(value, str)
                        and value.rpartition('@')[2] == image.rpartition('@')[2]
                        and canonical_docker_image_repository(value) == canonical_docker_image_repository(image)
                        for value in digests)
                and (declared is None or isinstance(declared, dict))):
                image_volumes = len(declared or {})
        except (ValueError, KeyError, TypeError):
            pass
    if (single_local_swarm_node(docker) != nodes
        or row('service', 'inspect', '--format', SERVICE_FORMAT, application) != service
        or identifiers('service', 'ps', '--no-trunc', '--quiet', service_id) != tasks
        or identifiers('ps', '-aq', '--no-trunc') != containers
        or identifiers('volume', 'ls', '--quiet') != volumes):
        raise InventoryChanged()
    for identifier, initial in task_rows.items():
        if row('inspect', '--type', 'task', '--format', TASK_FORMAT, identifier) != initial:
            raise InventoryChanged()
    for identifier, initial in container_rows.items():
        if row('inspect', '--type', 'container', '--format', CONTAINER_FORMAT, identifier) != initial:
            raise InventoryChanged()
    for name, initial in volume_rows.items():
        if row('volume', 'inspect', '--format', VOLUME_FORMAT, name) != initial:
            raise InventoryChanged()
    update = service['Update']
    if update is not None and not isinstance(update, dict):
        raise ValueError('Service update unavailable')
    service_quiescent = (replicas == 0 and owned == 0 and terminal
                 and (update or {}).get('State', '') in {'','completed','rollback_completed'})
    return {'status':'observed','reason_code':'resource_inventory_observed',
            'service_version':version,'desired_replicas':replicas,'task_count':len(tasks),
            'extant_container_count':owned,'configured_volume_count':configured_count,
            'referenced_volume_count':len(referenced),'nonempty_volume_count':nonempty,
            'empty_volume_count':empty,'unavailable_volume_count':unavailable,
            'unsupported_mounts_present':unsupported,'image_declared_volume_count':image_volumes,
            'service_quiescent':service_quiescent}

try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    result = observation(spec)
except ReadDenied:
    print(sys.argv[2] + json.dumps({'type':'denied'}), flush=True)
    sys.exit(0)
except InventoryChanged:
    result = {'reason_code':'resource_inventory_changed'}
except Exception:
    result = {'reason_code':'resource_inventory_unavailable'}
print(sys.argv[2] + json.dumps({'type':'complete','result':result}, sort_keys=True), flush=True)
"""
)
