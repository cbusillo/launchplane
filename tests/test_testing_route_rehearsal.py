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
    MAX_TRAEFIK_API_BYTES,
    READ_TRAEFIK_API,
    TRAEFIK_DYNAMIC_DIRECTORY,
)
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionCustody,
    TestingRouteExecutionIntent,
    ALLOCATE_TESTING_ROUTE_LOCK,
    FENCED_TESTING_ROUTE_WRITER,
    READ_TESTING_ROUTE_FENCE,
    parse_testing_route_fence,
    TestingRouteFence,
)
from control_plane.contracts.canonical_json import canonical_json_sha256


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
    def test_api_reader_works_with_no_bridge_or_published_port(self) -> None:
        proxy = "lp-testing-api-" + uuid4().hex[:12]
        try:
            docker(
                "run",
                "-d",
                "--pull=never",
                "--name",
                proxy,
                "--network",
                "none",
                os.environ["LAUNCHPLANE_TEST_TRAEFIK_IMAGE"],
                "--api.insecure=true",
            )
            deadline = time.monotonic() + 15
            while True:
                try:
                    raw = docker(
                        "exec",
                        "-e",
                        "http_proxy=http://unreachable.invalid:9",
                        proxy,
                        "sh",
                        "-c",
                        READ_TRAEFIK_API,
                        "_",
                        str(MAX_TRAEFIK_API_BYTES + 1),
                    )
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
            self.assertIsInstance(json.loads(raw), dict)
            self.assertLessEqual(len(raw.encode()), MAX_TRAEFIK_API_BYTES)
        finally:
            docker("rm", "-f", proxy)

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
                custody = TestingRouteExecutionCustody(
                    mount_source=str(root),
                    filesystem=docker(
                        "exec", proxy, "stat", "-c", "%d:%i", TRAEFIK_DYNAMIC_DIRECTORY
                    ),
                )
                epoch = docker(
                    "exec",
                    proxy,
                    "sh",
                    "-c",
                    "cat /proc/sys/kernel/random/boot_id; cat /proc/1/stat; if test -e /proc/self/timens_offsets; then sed -n '/^boottime/p' /proc/self/timens_offsets; else printf 'boottime 0 0\\n'; fi",
                ).splitlines()
                process_epoch = ":".join(
                    [epoch[0], epoch[1].rsplit(") ", 1)[-1].split()[19], *epoch[2].split()[1:]]
                )
                lock = docker(
                    "exec",
                    proxy,
                    "sh",
                    "-c",
                    ALLOCATE_TESTING_ROUTE_LOCK,
                    "_",
                    TRAEFIK_DYNAMIC_DIRECTORY,
                    route_file.name,
                    custody.filesystem,
                    process_epoch,
                )
                generation = hashlib.sha256(prefix.encode()).hexdigest()
                docker(
                    "exec",
                    "-i",
                    proxy,
                    "sh",
                    "-c",
                    FENCED_TESTING_ROUTE_WRITER,
                    "_",
                    TRAEFIK_DYNAMIC_DIRECTORY,
                    route_file.name,
                    custody.filesystem,
                    "initialize",
                    generation,
                    "0",
                    generation,
                    "absent",
                    generation,
                    "absent",
                    lock,
                    process_epoch,
                )

                def writer_arguments(
                    backend: str, operation: str, expected: str, mode: str = "publish"
                ) -> list[str]:
                    execution = TestingRouteExecutionIntent(
                        operation_id=operation,
                        server_id=proxy,
                        route_id=plan.route_id,
                        route_digest=plan.digest,
                        backend=backend,
                        document_sha256=canonical_json_sha256(route_document(plan, backend)),
                        custody=custody,
                    )
                    return [
                        "sh",
                        "-c",
                        FENCED_TESTING_ROUTE_WRITER,
                        "_",
                        TRAEFIK_DYNAMIC_DIRECTORY,
                        route_file.name,
                        custody.filesystem,
                        mode,
                        generation,
                        str(
                            TestingRouteFence.model_validate(
                                parse_testing_route_fence(
                                    docker(
                                        "exec",
                                        proxy,
                                        "sh",
                                        "-c",
                                        READ_TESTING_ROUTE_FENCE,
                                        "_",
                                        TRAEFIK_DYNAMIC_DIRECTORY,
                                        route_file.name,
                                        custody.filesystem,
                                    )
                                )["fence"]
                            ).revision
                        ),
                        execution.key(attempt=1),
                        expected,
                        execution.document_sha256,
                        generation,
                        lock,
                        process_epoch,
                    ]

                initial = json.dumps(
                    route_document(plan, old_url), sort_keys=True, separators=(",", ":")
                ).encode()
                docker(
                    "exec",
                    "-i",
                    proxy,
                    *writer_arguments(old_url, prefix + "-initial", "absent"),
                    content=initial,
                )
                docker("network", "connect", network, proxy)
                ports = json.loads(
                    docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", proxy)
                )
                api = "http://127.0.0.1:" + ports["8080/tcp"][0]["HostPort"]
                public = "http://127.0.0.1:" + ports["80/tcp"][0]["HostPort"]

                def observed() -> str | None:
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
                for index, backend in enumerate((old_url, new_url, old_url)):
                    desired = json.dumps(
                        route_document(plan, backend), sort_keys=True, separators=(",", ":")
                    ).encode()
                    arguments = writer_arguments(
                        backend,
                        prefix + "-" + str(index),
                        hashlib.sha256(route_file.read_bytes()).hexdigest(),
                    )
                    if backend != new_url:
                        docker("exec", "-i", proxy, *arguments, content=desired)
                    else:
                        # Disconnect the real Docker exec client while the
                        # original managed writer can still rename its file.
                        binary = root / ".writer-bin"
                        binary.mkdir()
                        entered, release = root / "entered", root / "release"
                        gate = binary / "mv"
                        gate.write_text(
                            "#!/bin/sh\nfor destination do :; done\n"
                            'if test "$destination" = "$ROUTE_FILE"; then\n'
                            ' printf entered > "$GATE_ENTERED"\n'
                            ' while test ! -e "$GATE_RELEASE"; do sleep .02; done\n'
                            'fi\nexec /bin/mv "$@"\n'
                        )
                        gate.chmod(0o700)
                        process = subprocess.Popen(
                            [
                                "docker",
                                "exec",
                                "-i",
                                "-e",
                                "PATH="
                                + TRAEFIK_DYNAMIC_DIRECTORY
                                + "/.writer-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                                "-e",
                                "ROUTE_FILE=" + route_file.name,
                                "-e",
                                "GATE_ENTERED=" + TRAEFIK_DYNAMIC_DIRECTORY + "/entered",
                                "-e",
                                "GATE_RELEASE=" + TRAEFIK_DYNAMIC_DIRECTORY + "/release",
                                proxy,
                                *arguments,
                            ],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        try:
                            assert process.stdin is not None
                            process.stdin.write(desired)
                            process.stdin.close()
                            process.stdin = None
                            deadline = time.monotonic() + 10
                            while not entered.exists():
                                if process.poll() is not None or time.monotonic() >= deadline:
                                    details = (
                                        process.communicate(timeout=5)
                                        if process.poll() is not None
                                        else (b"", b"")
                                    )
                                    self.fail(
                                        "The isolated Docker writer did not reach its rename gate: "
                                        + repr(details)
                                    )
                                stop.wait(0.01)
                            process.kill()
                            process.communicate(timeout=5)
                            settlement = arguments.copy()
                            settlement[settlement.index("publish")] = "settle"
                            self.assertEqual(
                                docker("exec", "-i", proxy, *settlement, content=b""),
                                "busy",
                            )
                            self.assertEqual(observed(), old_url)
                            release.touch()
                            deadline = time.monotonic() + 10
                            while True:
                                outcome = docker("exec", "-i", proxy, *settlement, content=b"")
                                if outcome != "busy":
                                    self.assertEqual(outcome, "applied")
                                    break
                                if time.monotonic() >= deadline:
                                    self.fail("The original isolated writer did not finish")
                                stop.wait(0.01)
                        finally:
                            release.touch()
                            if process.poll() is None:
                                process.kill()
                            process.communicate(timeout=5)
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
