"""Opt-in real isolated Swarm/Compose targets, Traefik and continuous HTTP probes.

Dokploy's HTTP edge is replaced by target-specific Docker calls; the production
adapter, persisted controller, real target runtime and atomic publisher run.
The disposable nested engine never changes the workstation's Swarm membership.
"""

from contextlib import nullcontext
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from typing import Any, cast
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from control_plane.contracts.generic_web_cutover import GenericWebCutoverPlan, WebSlot
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.dokploy.generic_web_cutover import DokployWebCutoverProvider, WebSlotDriver
from control_plane.dokploy.traefik_cutover import (
    TraefikFileRoute,
    TraefikRouteTarget,
    route_payload,
)
from control_plane.generic_web_cutover import WebDeploymentCutoverStore
from control_plane.release_compatibility import classify_release_database_compatibility
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_cutover import execute_web_cutover, rollback_web_cutover
from tests.test_generic_web_cutover import plan, deployment, Provider


def docker(*args: str, timeout: int = 120) -> str:
    return subprocess.run(
        ["docker", *args], text=True, capture_output=True, check=True, timeout=timeout
    ).stdout.strip()


SERVER = """import os,json,time,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
identity=json.loads(os.environ['LAUNCHPLANE_RUNTIME_IDENTITY_JSON'])
active=0
lock=threading.Lock()
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  global active
  if self.path=='/active':
   with lock: body=str(active).encode()
   self.send_response(200);self.end_headers();self.wfile.write(body);return
  with lock: active+=1
  try:
   if self.path=='/slow': time.sleep(2)
   body=json.dumps(identity).encode() if self.path=='/health' else (identity['source_git_ref']+' body{color:green}').encode()
   self.send_response(200);self.end_headers();self.wfile.write(body)
  finally:
   with lock: active-=1
 def log_message(self,*args): pass
ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
"""


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_WEB_CUTOVER") == "1",
    "isolated nested-engine rehearsal not selected",
)
class WebCutoverRehearsalTests(unittest.TestCase):
    def test_both_shapes_release_faults_recovery_and_switch_back_with_no_5xx(self) -> None:
        for shape in ("application", "compose"):
            with self.subTest(shape=shape):
                self.rehearse(shape)

    def rehearse(self, shape: str) -> None:
        prefix = "lp3267-" + uuid4().hex[:10]
        engine, proxy, network = prefix + "-engine", prefix + "-proxy", prefix + "-net"
        stop = threading.Event()
        thread: threading.Thread | None = None
        observations: list[dict[str, object]] = []
        Path("state").mkdir(exist_ok=True)
        with TemporaryDirectory(dir=Path("state")) as folder:
            root = Path(folder).resolve()
            dynamic = root / "dynamic"
            dynamic.mkdir()
            try:
                docker("network", "create", network)
                docker(
                    "run",
                    "-d",
                    "--privileged",
                    "--name",
                    engine,
                    "--network",
                    network,
                    "-e",
                    "DOCKER_TLS_CERTDIR=",
                    os.environ["LAUNCHPLANE_TEST_DIND_IMAGE"],
                    "dockerd",
                    "--host=unix:///var/run/docker.sock",
                    "--storage-driver=vfs",
                )

                def nested(*args: str, timeout: int = 120) -> str:
                    return docker("exec", engine, "docker", *args, timeout=timeout)

                deadline = time.monotonic() + 45
                while True:
                    try:
                        nested("info")
                        break
                    except subprocess.CalledProcessError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.2)
                if shape == "application":
                    nested("swarm", "init", "--advertise-addr", "eth0")
                images = tuple(
                    os.environ[name]
                    for name in ("LAUNCHPLANE_TEST_WEB_OLD_IMAGE", "LAUNCHPLANE_TEST_WEB_NEW_IMAGE")
                )
                for image in images:
                    nested("pull", image, timeout=240)
                original = plan(shape)
                manifests = []
                slots = []
                for index, (slot, manifest, image) in enumerate(
                    zip(
                        (original.old, original.new),
                        (original.production, original.candidate),
                        images,
                        strict=True,
                    )
                ):
                    repository, digest = image.split("@")
                    manifest = manifest.model_copy(deep=True)
                    manifest.image.repository, manifest.image.digest = repository, digest
                    manifests.append(manifest)
                    identity = slot.runtime_identity.model_copy(
                        update={"image_reference": image, "image_digest": digest}
                    )
                    slots.append(
                        slot.model_copy(
                            update={
                                "private_url": f"http://{engine}:{18080 + index}",
                                "image": image,
                                "deploy_reference": "",
                                "runtime_identity": identity,
                            }
                        )
                    )
                coordinates = tuple(
                    type(original.lane.slots[0]).model_validate(
                        slot.model_dump(include=set(type(original.lane.slots[0]).model_fields))
                    )
                    for slot in slots
                )
                intent = GenericWebCutoverPlan.model_validate(
                    {
                        **original.model_dump(),
                        "old": slots[0],
                        "new": slots[1],
                        "production": manifests[0],
                        "candidate": manifests[1],
                        "lane": original.lane.model_copy(
                            update={"slots": coordinates, "writers": ()}
                        ),
                        "compatibility": classify_release_database_compatibility(
                            production=manifests[0], candidate=manifests[1]
                        ),
                    }
                )

                def start(slot: WebSlot) -> None:
                    port = 18080 if slot.slot_id == "old" else 18081
                    identity = slot.runtime_identity.model_dump_json()
                    if shape == "application":
                        # A genuinely separate Swarm service per slot, not a
                        # rolling update of the serving service.
                        name = slot.target_id
                        if nested("service", "ls", "-q", "--filter", "name=" + name):
                            return
                        nested(
                            "service",
                            "create",
                            "--no-resolve-image",
                            "--name",
                            name,
                            "--publish",
                            f"mode=host,published={port},target=8080",
                            "--env",
                            "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity,
                            slot.image,
                            "python",
                            "-c",
                            SERVER,
                        )
                    else:
                        compose = root / (slot.slot_id + ".json")
                        compose.write_text(
                            json.dumps(
                                {
                                    "services": {
                                        "app": {
                                            "image": slot.image,
                                            "command": ["python", "-c", SERVER],
                                            "environment": {
                                                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON": identity
                                            },
                                            "ports": [f"{port}:8080"],
                                        }
                                    }
                                }
                            )
                        )
                        docker("cp", str(compose), engine + ":/" + slot.slot_id + ".json")
                        nested(
                            "compose",
                            "-p",
                            slot.target_id,
                            "-f",
                            "/" + slot.slot_id + ".json",
                            "up",
                            "-d",
                            "--pull",
                            "never",
                        )
                    deadline = time.monotonic() + 45
                    while True:
                        try:
                            private(slot)
                            return
                        except subprocess.CalledProcessError:
                            if time.monotonic() > deadline:
                                raise
                            time.sleep(0.2)

                def private(slot: WebSlot) -> dict[str, Any]:
                    return cast(
                        dict[str, Any],
                        json.loads(
                            docker("exec", engine, "wget", "-qO-", slot.private_url + "/health")
                        ),
                    )

                start(intent.old)
                target = TraefikRouteTarget(
                    route_id=intent.lane.route_id,
                    host=intent.lane.host,
                    entrypoints=("web",),
                    route_file=dynamic / "route.json.yaml",
                    api_url="http://127.0.0.1",
                    tls=False,
                )
                target.route_file.write_text(
                    json.dumps(route_payload(target, intent.old.private_url))
                )
                docker(
                    "run",
                    "-d",
                    "--name",
                    proxy,
                    "--network",
                    network,
                    "-p",
                    "127.0.0.1::80",
                    "-p",
                    "127.0.0.1::8080",
                    "-v",
                    str(dynamic) + ":/dynamic:ro",
                    os.environ["LAUNCHPLANE_TEST_TRAEFIK_IMAGE"],
                    "--api.insecure=true",
                    "--providers.file.directory=/dynamic",
                    "--providers.file.watch=true",
                    "--providers.providersThrottleDuration=100ms",
                    "--entrypoints.web.address=:80",
                )
                ports = json.loads(
                    docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", proxy)
                )
                public = "http://127.0.0.1:" + ports["80/tcp"][0]["HostPort"]
                api = "http://127.0.0.1:" + ports["8080/tcp"][0]["HostPort"]
                route = TraefikFileRoute(
                    target.model_copy(update={"api_url": api}), apply_timeout_seconds=10
                )
                opener = build_opener(ProxyHandler({}))

                def read(path: str) -> tuple[int, str]:
                    with opener.open(
                        Request(public + path, headers={"Host": intent.lane.host}), timeout=5
                    ) as response:
                        return response.status, response.read().decode()

                deadline = time.monotonic() + 30
                while True:
                    try:
                        self.assertEqual(read("/health")[0], 200)
                        self.assertEqual(route.observed_target(), intent.old.private_url)
                        break
                    except Exception:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.1)

                def probe() -> None:
                    while not stop.is_set():
                        now = datetime.now(UTC).isoformat()
                        try:
                            status, body = read("/health")
                            observations.append(
                                {"at": now, "status": status, "identity": json.loads(body)}
                            )
                            asset_status, _ = read("/static/app.css")
                            if asset_status != 200:
                                observations.append({"at": now, "error": "asset unavailable"})
                        except Exception as error:
                            observations.append({"at": now, "error": type(error).__name__})
                        stop.wait(0.02)

                thread = threading.Thread(target=probe)
                thread.start()
                fixture = Provider(intent)
                fail_ready = False
                wrong_digest = False

                def ready(p: GenericWebCutoverPlan, slot: WebSlot) -> Any:
                    if fail_ready:
                        raise ValueError("injected readiness failure")
                    observed = private(slot)
                    self.assertEqual(observed, slot.runtime_identity.model_dump(mode="json"))
                    # Independent Docker runtime digest check, including actual
                    # Swarm task containers rather than service config alone.
                    containers = nested(
                        "ps", "-q", "--filter", "ancestor=" + slot.image
                    ).splitlines()
                    self.assertTrue(containers)
                    for container in containers:
                        self.assertEqual(
                            nested("inspect", "--format", "{{.Image}}", container),
                            nested("image", "inspect", "--format", "{{.Id}}", slot.image),
                        )
                    durations = []
                    for path in ("/", "/static/app.css", "/"):
                        began = time.perf_counter()
                        body = docker(
                            "exec",
                            engine,
                            "wget",
                            "-qO-",
                            "--header",
                            "Host: " + p.lane.host,
                            slot.private_url + path,
                        )
                        durations.append((time.perf_counter() - began) * 1000)
                        self.assertIn(slot.runtime_identity.source_git_ref, body)
                    return fixture.readiness(p, slot).model_copy(
                        update={
                            "image": intent.old.image if wrong_digest else slot.image,
                            "first_request_ms": durations[0],
                            "warmed_request_ms": durations[-1],
                        }
                    )

                def health(slot: WebSlot, payload: Any, url: str) -> HealthcheckEvidence:
                    from control_plane.contracts.runtime_identity import RuntimeIdentity

                    return HealthcheckEvidence(
                        verified=True,
                        status="pass",
                        timeout_seconds=5,
                        urls=(url,),
                        observed_runtime_identity=RuntimeIdentity.model_validate(payload),
                    )

                def drain(_plan: GenericWebCutoverPlan, slot: WebSlot) -> bool:
                    deadline = time.monotonic() + 10
                    while True:
                        if (
                            docker("exec", engine, "wget", "-qO-", slot.private_url + "/active")
                            == "0"
                        ):
                            return True
                        if time.monotonic() > deadline:
                            return False
                        time.sleep(0.02)

                provider = DokployWebCutoverProvider(
                    intent,
                    host="https://synthetic-provider.invalid",
                    token="inert",
                    route=route,
                    driver=WebSlotDriver(
                        exclusive=lambda _p: nullcontext(),
                        preflight=lambda _p: None,
                        stage_runtime=lambda _p: intent.lane.runtime_settings_sha256,
                        readiness=ready,
                        private_health=lambda _p, slot: health(
                            slot, private(slot), slot.private_url + "/health"
                        ),
                        public_health=lambda _p: health(
                            intent.new, json.loads(read("/health")[1]), public + "/health"
                        ),
                        handoff=lambda _p, slot: {w: slot.target_id for w in intent.lane.writers},
                        drain=drain,
                    ),
                )
                store = WebDeploymentCutoverStore(
                    FilesystemRecordStore(root / "records"), deployment(intent)
                )

                def deploy(**kwargs: Any) -> None:
                    self.assertEqual(kwargs["resolved_target"].target_id, intent.new.target_id)
                    self.assertEqual(kwargs["resolved_target"].target_type, shape)
                    self.assertEqual(route.observed_target(), intent.old.private_url)
                    start(intent.new)

                with patch(
                    "control_plane.dokploy.generic_web_cutover.execute_dokploy_artifact_deploy",
                    side_effect=deploy,
                ):
                    fail_ready = True
                    with self.assertRaises(ValueError):
                        execute_web_cutover(intent, store, provider, record_id="release")
                    self.assertEqual(route.observed_target(), intent.old.private_url)
                    fail_ready = False
                    wrong_digest = True
                    with self.assertRaises(ValueError):
                        execute_web_cutover(intent, store, provider, record_id="release")
                    self.assertEqual(route.observed_target(), intent.old.private_url)
                    wrong_digest = False
                    with patch.object(
                        route, "observed_target", return_value="http://foreign.invalid:9999"
                    ):
                        with self.assertRaises(ValueError):
                            execute_web_cutover(intent, store, provider, record_id="release")
                    self.assertEqual(route.observed_target(), intent.old.private_url)
                    original_publish = route.publish

                    def uncertain(backend: str, *, expected: tuple[str, ...]) -> str:
                        original_publish(backend, expected=expected)
                        raise TimeoutError("injected lost publish readback")

                    with patch.object(route, "publish", side_effect=uncertain):
                        with self.assertRaises(TimeoutError):
                            execute_web_cutover(intent, store, provider, record_id="release")
                    self.assertEqual(store.load("release").step, "switching")
                    # Reopen persistent storage to represent a controller crash.
                    store = WebDeploymentCutoverStore(
                        FilesystemRecordStore(root / "records"), deployment(intent)
                    )
                    # Desktop bind-mount events can be coalesced. Exercise bounded
                    # restart recovery; each attempt must retain its checkpoint.
                    for attempt in range(3):
                        try:
                            result = execute_web_cutover(
                                intent, store, provider, record_id="release"
                            )
                            break
                        except TimeoutError:
                            self.assertEqual(store.load("release").step, "switching")
                            if attempt == 2:
                                raise
                    self.assertEqual(result.step, "complete")
                    self.assertEqual(
                        json.loads(read("/health")[1]),
                        intent.new.runtime_identity.model_dump(mode="json"),
                    )
                    deadline = time.monotonic() + 5
                    while not any(
                        cast(dict[str, Any], sample.get("identity", {})).get("slot") == "new"
                        for sample in observations
                    ):
                        if time.monotonic() > deadline:
                            self.fail("probe did not observe candidate traffic")
                        time.sleep(0.02)
                    try:
                        rollback_web_cutover(store, provider, "release")
                    except TimeoutError:
                        self.assertEqual(store.load("release").step, "rolling_back")
                        store = WebDeploymentCutoverStore(
                            FilesystemRecordStore(root / "records"), deployment(intent)
                        )
                        rollback_web_cutover(store, provider, "release")
                    self.assertEqual(
                        json.loads(read("/health")[1]),
                        intent.old.runtime_identity.model_dump(mode="json"),
                    )
                stop.set()
                thread.join(10)
                self.assertFalse(thread.is_alive())
                self.assertTrue(observations)
                self.assertTrue(
                    {
                        cast(dict[str, Any], sample.get("identity", {})).get("slot")
                        for sample in observations
                    }
                    <= {"old", "new"}
                )
                self.assertFalse([sample for sample in observations if sample.get("status") != 200])
                receipt = {
                    "shape": shape,
                    "samples": observations,
                    "errors": 0,
                    "old_image": intent.old.image,
                    "new_image": intent.new.image,
                    "final_record": store.load("release").model_dump(mode="json"),
                }
                evidence = os.environ.get("LAUNCHPLANE_TEST_WEB_EVIDENCE")
                if evidence:
                    Path(evidence).mkdir(parents=True, exist_ok=True)
                    (Path(evidence) / (shape + ".json")).write_text(json.dumps(receipt, indent=2))
            except Exception:
                diagnostic = os.environ.get("LAUNCHPLANE_TEST_WEB_EVIDENCE")
                if diagnostic:
                    Path(diagnostic).mkdir(parents=True, exist_ok=True)
                    logs = subprocess.run(
                        ["docker", "logs", proxy], capture_output=True, text=True, timeout=20
                    )
                    (Path(diagnostic) / (shape + "-failure.log")).write_text(
                        logs.stdout + logs.stderr
                    )
                raise
            finally:
                stop.set()
                if thread is not None:
                    thread.join(10)
                for container in (proxy, engine):
                    subprocess.run(
                        ["docker", "rm", "-fv", container], capture_output=True, timeout=60
                    )
                subprocess.run(
                    ["docker", "network", "rm", network], capture_output=True, timeout=30
                )
