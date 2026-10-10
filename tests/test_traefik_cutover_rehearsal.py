"""Opt-in real file-provider publication and external HTTP continuity proof."""

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

from control_plane.dokploy.traefik_cutover import (
    TraefikFileRoute,
    TraefikRouteTarget,
    route_payload,
)


def docker(*arguments: str) -> str:
    return subprocess.run(
        ["docker", *arguments], text=True, capture_output=True, check=True, timeout=90
    ).stdout.strip()


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_TRAEFIK_IMAGE"), "isolated Traefik image not selected"
)
class TraefikCutoverRehearsalTests(unittest.TestCase):
    def test_atomic_applied_switch_with_continuous_external_reads_and_retained_old(self) -> None:
        with TemporaryDirectory(dir=Path("state")) as directory:
            root = Path(directory).resolve()
            prefix = "lp-cutover-" + uuid4().hex[:12]
            network = prefix + "-network"
            proxy_network = prefix + "-proxy-network"
            containers: list[str] = []
            docker("network", "create", "--internal", network)
            docker("network", "create", proxy_network)
            samples: list[str] = []
            errors: list[str] = []
            stop = threading.Event()
            reader: threading.Thread | None = None
            try:
                for slot in ("old", "new", "prepared"):
                    name = prefix + "-" + slot
                    program = (
                        "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
                        "class H(BaseHTTPRequestHandler):\n"
                        " def do_GET(self):\n"
                        f"  body={slot.encode()!r};self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)\n"
                        " def log_message(self,*args):pass\n"
                        "HTTPServer(('0.0.0.0',8080),H).serve_forever()\n"
                    )
                    containers.append(name)
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
                proxy = prefix + "-proxy"
                containers.append(proxy)
                target = TraefikRouteTarget(
                    route_id="fixture",
                    host="fixture.invalid",
                    entrypoints=("web",),
                    route_file=root / "fixture.yaml",
                    api_url="http://127.0.0.1",
                    tls=False,
                )
                old_url = "http://" + prefix + "-old:8080"
                target.route_file.write_text(json.dumps(route_payload(target, old_url)))
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
                    str(root) + ":/dynamic:ro",
                    os.environ["LAUNCHPLANE_TEST_TRAEFIK_IMAGE"],
                    "--api.insecure=true",
                    "--providers.file.directory=/dynamic",
                    "--providers.file.watch=true",
                    "--entrypoints.web.address=:80",
                )
                docker("network", "connect", network, proxy)
                ports = json.loads(
                    docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", proxy)
                )
                api = "http://127.0.0.1:" + ports["8080/tcp"][0]["HostPort"]
                public = "http://127.0.0.1:" + ports["80/tcp"][0]["HostPort"]
                target = target.model_copy(update={"api_url": api})
                route = TraefikFileRoute(target)
                deadline = time.monotonic() + 20
                while True:
                    try:
                        if route.observed_target() == old_url:
                            break
                    except (OSError, ValueError):
                        pass
                    if time.monotonic() >= deadline:
                        self.fail(
                            "isolated Traefik startup/readback did not settle: "
                            + docker("logs", proxy)
                        )
                    time.sleep(0.1)

                def probe() -> None:
                    opener = build_opener(ProxyHandler({}))
                    while not stop.is_set():
                        try:
                            with opener.open(
                                Request(public, headers={"Host": target.host}), timeout=2
                            ) as response:
                                if response.status != 200:
                                    raise ValueError("non-200 public response")
                                samples.append(response.read().decode())
                        except Exception as error:
                            errors.append(type(error).__name__)
                        stop.wait(0.02)

                reader = threading.Thread(target=probe)
                reader.start()
                for slot in ("prepared", "new", "old"):
                    destination = "http://" + prefix + "-" + slot + ":8080"
                    previous = route.observed_target()
                    try:
                        route.publish(destination, expected=(previous,))
                    except ValueError:
                        # An async apply is an uncertain result, never a green
                        # acknowledgement. Reconcile by independent readback.
                        pass
                    deadline = time.monotonic() + 10
                    while route.observed_target() != destination:
                        if time.monotonic() >= deadline:
                            self.fail("atomic file-provider publication remained unverified")
                        time.sleep(0.05)
                    time.sleep(0.15)
                stop.set()
                reader.join(timeout=3)
                self.assertFalse(reader.is_alive())
                self.assertFalse(errors, errors)
                self.assertTrue({"old", "prepared", "new"} <= set(samples), samples)
                self.assertEqual(
                    json.loads(target.route_file.read_text()),
                    json.loads(json.dumps(route_payload(target, old_url))),
                )
                self.assertEqual(
                    docker("inspect", "--format", "{{.State.Running}}", prefix + "-old"), "true"
                )
            finally:
                stop.set()
                if reader:
                    reader.join(timeout=3)
                for name in reversed(containers):
                    docker("rm", "-f", name)
                docker("network", "rm", network)
                docker("network", "rm", proxy_network)
