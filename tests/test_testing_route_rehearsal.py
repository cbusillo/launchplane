"""Real Traefik application and Linux atomic writer; no managed/live targets."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from control_plane.dokploy.testing_route import (
    compile_testing_route,
    observe_testing_route,
    route_document,
    TestingRoutePending,
    TestingLaneRoute,
)
from control_plane.dokploy.testing_route_transport import (
    ATOMIC_TESTING_ROUTE_WRITER,
    TRAEFIK_DYNAMIC_DIRECTORY,
)


def docker(*arguments: str, content: bytes | None = None) -> str:
    return (
        subprocess.run(
            ["docker", *arguments], input=content, capture_output=True, check=True, timeout=90
        )
        .stdout.decode()
        .strip()
    )


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_TRAEFIK_IMAGE"), "isolated Traefik image not selected"
)
class TestingRouteRehearsalTests(unittest.TestCase):
    def test_owned_route_switch_and_inverse_keep_public_requests_serving(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            prefix = "lp-testing-route-" + uuid4().hex[:12]
            network, proxy_network = prefix + "-data", prefix + "-proxy"
            containers: list[str] = []
            networks: list[str] = []
            stop = threading.Event()
            samples: list[str] = []
            errors: list[str] = []
            reader: threading.Thread | None = None
            opener = build_opener(ProxyHandler({}))
            try:
                for name, internal in ((network, True), (proxy_network, False)):
                    docker("network", "create", *(["--internal"] if internal else []), name)
                    networks.append(name)
                for copy in ("old", "new"):
                    name = prefix + "-" + copy
                    program = (
                        "from http.server import BaseHTTPRequestHandler,HTTPServer\nclass H(BaseHTTPRequestHandler):\n def do_GET(self):\n  body="
                        + repr(copy.encode())
                        + ";self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)\n def log_message(self,*args):pass\nHTTPServer(('0.0.0.0',8080),H).serve_forever()\n"
                    )
                    docker(
                        "run",
                        "-d",
                        "--pull=never",
                        "--name",
                        name,
                        "--network",
                        network,
                        "--entrypoint",
                        "python",
                        os.environ.get("LAUNCHPLANE_TEST_HTTP_IMAGE", "python:3-alpine"),
                        "-c",
                        program,
                    )
                    containers.append(name)
                old_url, new_url = (
                    "http://" + prefix + "-" + copy + ":8080" for copy in ("old", "new")
                )
                raw = {
                    "routers": {
                        "prior@docker": {
                            "rule": "Host(`fixture.invalid`)",
                            "entryPoints": ["web"],
                            "service": "prior@docker",
                            "status": "enabled",
                        }
                    },
                    "services": {
                        "prior@docker": {
                            "status": "enabled",
                            "loadBalancer": {"servers": [{"url": old_url}]},
                        }
                    },
                }
                plan = compile_testing_route(
                    product="rehearsal",
                    context=prefix,
                    host="fixture.invalid",
                    old_backend=old_url,
                    new_backend=new_url,
                    raw=raw,
                    public_entrypoints=("web",),
                )
                plan = TestingLaneRoute.model_validate(
                    {**plan.model_dump(), "legacy_routers": {}, "legacy_backend": ""}
                )
                route_file = root / (plan.route_id + ".yml")
                proxy = prefix + "-traefik"
                docker(
                    "run",
                    "-d",
                    "--pull=never",
                    "--name",
                    proxy,
                    "--network",
                    proxy_network,
                    "-p",
                    "127.0.0.1::8080",
                    "-p",
                    "127.0.0.1::80",
                    "-v",
                    str(root) + ":" + TRAEFIK_DYNAMIC_DIRECTORY,
                    os.environ["LAUNCHPLANE_TEST_TRAEFIK_IMAGE"],
                    "--api.insecure=true",
                    "--providers.file.directory=" + TRAEFIK_DYNAMIC_DIRECTORY,
                    "--providers.file.watch=true",
                    "--entrypoints.web.address=:80",
                )
                containers.append(proxy)
                initial = json.dumps(
                    route_document(plan, old_url), sort_keys=True, separators=(",", ":")
                ).encode()
                docker(
                    "exec",
                    "-i",
                    proxy,
                    "sh",
                    "-c",
                    ATOMIC_TESTING_ROUTE_WRITER,
                    "_",
                    TRAEFIK_DYNAMIC_DIRECTORY,
                    route_file.name,
                    "absent",
                    hashlib.sha256(initial).hexdigest(),
                    content=initial,
                )
                docker("network", "connect", network, proxy)
                ports = json.loads(
                    docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", proxy)
                )
                api = "http://127.0.0.1:" + ports["8080/tcp"][0]["HostPort"]
                public = "http://127.0.0.1:" + ports["80/tcp"][0]["HostPort"]

                def observed() -> str:
                    with opener.open(api + "/api/rawdata", timeout=3) as response:
                        applied = json.load(response)
                    return observe_testing_route(plan, applied, json.loads(route_file.read_text()))

                def wait_for(backend: str) -> None:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            if observed() == backend:
                                return
                        except (OSError, ValueError, TestingRoutePending):
                            pass
                        if time.monotonic() >= deadline:
                            self.fail("Isolated applied route did not converge")
                        stop.wait(0.05)

                wait_for(old_url)

                def probe() -> None:
                    while not stop.is_set():
                        try:
                            with opener.open(
                                Request(public, headers={"Host": plan.host}), timeout=2
                            ) as response:
                                if response.status != 200:
                                    raise ValueError("HTTP status")
                                samples.append(response.read().decode())
                        except Exception as error:
                            errors.append(type(error).__name__)
                        stop.wait(0.02)

                reader = threading.Thread(target=probe)
                reader.start()
                for backend in (old_url, new_url, old_url):
                    desired = json.dumps(
                        route_document(plan, backend), sort_keys=True, separators=(",", ":")
                    ).encode()
                    docker(
                        "exec",
                        "-i",
                        proxy,
                        "sh",
                        "-c",
                        ATOMIC_TESTING_ROUTE_WRITER,
                        "_",
                        TRAEFIK_DYNAMIC_DIRECTORY,
                        route_file.name,
                        hashlib.sha256(route_file.read_bytes()).hexdigest(),
                        hashlib.sha256(desired).hexdigest(),
                        content=desired,
                    )
                    wait_for(backend)
                    expected = "old" if backend == old_url else "new"
                    deadline = time.monotonic() + 5
                    while expected not in samples:
                        if time.monotonic() >= deadline:
                            self.fail("Public request never observed the applied copy")
                        stop.wait(0.02)
                stop.set()
                reader.join(timeout=3)
                self.assertFalse(reader.is_alive())
                self.assertFalse(errors, errors)
                self.assertTrue({"old", "new"} <= set(samples))
                self.assertEqual(
                    docker("inspect", "--format", "{{.State.Running}}", prefix + "-old"), "true"
                )
                self.assertEqual(observed(), old_url)
            finally:
                stop.set()
                if reader is not None:
                    reader.join(timeout=3)
                for container in reversed(containers):
                    docker("rm", "-f", container)
                for name in reversed(networks):
                    docker("network", "rm", name)
