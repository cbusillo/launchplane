import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

from control_plane.dokploy.cutover_host_files import MANAGED_PROXY_CONTAINER

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
state=json.loads(Path(os.environ['FAKE_PRODUCT_HOST_STATE']).read_text())
arguments=sys.argv[1:]
def fail():
    sys.exit(1)
def applied_backend(route_file):
    try: document=json.loads(Path(route_file).read_text())
    except (OSError,ValueError): return None
    services=document['http']['services']
    return next(iter(services.values()))['loadBalancer']['servers'][0]['url']
if arguments[:1]==['inspect']:
    container=state['containers'].get(arguments[1])
    print(json.dumps([container] if container else [])) if container else fail()
elif arguments[:1]==['info']:
    print(json.dumps(state['docker_root']))
elif arguments[:2]==['volume','inspect']:
    volume=state['volumes'].get(arguments[-1])
    print(json.dumps(volume)) if volume else fail()
elif arguments[:1]==['exec'] and arguments[2:4]==['stat','-c']:
    source=state['mounts'].get(arguments[-1])
    if source is None: fail()
    metadata=os.stat(source)
    print(f'{metadata.st_dev}:{metadata.st_ino}')
elif arguments[:1]==['exec'] and arguments[2]=='cat':
    print(state['proxy_files'][arguments[3]])
elif arguments[:1]==['exec'] and arguments[2]=='wget':
    url=arguments[-1]; route=state['route']
    backend=applied_backend(route['file'])
    if backend is None: fail()
    if url.endswith('/api/rawdata'):
        print(json.dumps({'routers':{route['id']+'@file':{'rule':'Host(`'+route['host']+'`)','service':route['id'],'status':'enabled','entryPoints':route['entrypoints'],'middlewares':[]}}}))
    elif url.endswith('/api/http/services/'+route['id']+'@file'):
        print(json.dumps({'status':'enabled','loadBalancer':{'servers':[{'url':backend}]}}))
    else: fail()
else:
    fail()
"""


class FakeProductHost:
    def __init__(
        self, root: Path, *, route_id: str = "lane", host: str = "fixture.invalid"
    ) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.dynamic = root / "host" / "traefik-dynamic"
        self.dynamic.mkdir(parents=True)
        self.authority = root / "host" / "authority"
        self.authority.mkdir()
        self.docker_root = root / "host" / "docker"
        self.volume = "lane-prepared"
        self.volume_data = self.docker_root / "volumes" / self.volume / "_data"
        self.volume_data.mkdir(parents=True)
        self.route_file = self.dynamic / f"{route_id}.yml"
        self.state: dict[str, Any] = {
            "containers": {
                MANAGED_PROXY_CONTAINER: {
                    "Id": "proxy",
                    "Name": "/" + MANAGED_PROXY_CONTAINER,
                    "State": {"Running": True},
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": str(self.dynamic),
                            "Destination": "/etc/dokploy/traefik/dynamic",
                            "RW": True,
                        }
                    ],
                }
            },
            "mounts": {"/etc/dokploy/traefik/dynamic": str(self.dynamic)},
            "proxy_files": {
                "/etc/traefik/traefik.yml": json.dumps(
                    {
                        "providers": {
                            "file": {"directory": "/etc/dokploy/traefik/dynamic", "watch": True}
                        }
                    }
                )
            },
            "docker_root": str(self.docker_root),
            "volumes": {
                self.volume: {
                    "Name": self.volume,
                    "Driver": "local",
                    "Options": None,
                    "Mountpoint": str(self.volume_data),
                }
            },
            "route": {
                "file": str(self.route_file),
                "id": route_id,
                "host": host,
                "entrypoints": ["web"],
            },
        }
        self.state_file = root / "fake-host-state.json"
        self.save()
        self.effects: list[tuple[str, ...]] = []

    def save(self) -> None:
        self.state_file.write_text(json.dumps(self.state))

    def read(self, argv: tuple[str, ...], stdin: str | None) -> str:
        environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_PRODUCT_HOST_STATE": str(self.state_file),
        }
        command = (
            [sys.executable, *argv[1:]]
            if argv[0] == "python3"
            else [str(self.bin / "docker"), *argv[1:]]
        )
        return subprocess.run(
            command, input=stdin, text=True, capture_output=True, check=True, env=environment
        ).stdout

    def execute(self, argv: tuple[str, ...], stdin: str | None) -> str:
        self.effects.append(argv)
        return self.read(argv, stdin)
