"""Isolated Docker/HTTP rehearsal. Requires the local Docker Desktop test engine."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import uuid
from unittest.mock import patch

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control_plane.contracts.product_environment_read_model import build_product_activity_read_model
from control_plane.contracts.lane_service_restart import (
    LaneServiceRestartRecovery,
    LaneServiceRestartRecoveryRequest,
    LaneServiceRestartResponse,
)
from control_plane.contracts.runtime_identity import runtime_identity_env
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_lane_service_restart import seed_lane
from tests.support.auth import StubVerifier, identity, local_operator_policy
from control_plane.service_auth import BearerIdentityConfig
from tests.test_service import create_launchplane_fastapi_test_app


def docker(*args: str) -> str:
    return subprocess.check_output(
        ["docker", "--context", "desktop-linux", *args], text=True, timeout=45
    ).strip()


def main() -> None:
    if docker("info", "--format", "{{.Name}}") != "docker-desktop":
        raise RuntimeError("This rehearsal only uses the local Docker Desktop test engine.")
    image = docker("image", "inspect", "python:3-alpine", "--format", "{{index .RepoDigests 0}}")
    project = "lp-restart-proof-" + uuid.uuid4().hex[:12]
    ids: list[str] = []
    writes: list[str] = []
    with (
        TemporaryDirectory(prefix="lp-restart-", dir=os.environ.get("TMPDIR")) as temporary,
        ExitStack() as stack,
    ):
        root = Path(temporary)
        store = PostgresRecordStore(database_url="sqlite+pysqlite:///" + str(root / "state.db"))
        stack.callback(store.close)
        store.ensure_schema()
        expected = seed_lane(store, image=image)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            web_port = reservation.getsockname()[1]
        command = """import os,json
from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps({"status":"ok","runtime_identity":json.loads(os.environ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])}).encode()
  self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(body)
 def log_message(self,*args): pass
HTTPServer(("0.0.0.0",8000),Handler).serve_forever()
"""
        arguments = [
            "run",
            "-d",
            "--pull=never",
            "--name",
            project + "-web",
            "--label",
            "com.docker.compose.project=" + project,
            "--label",
            "com.docker.compose.service=web",
            "--label",
            "com.docker.compose.oneoff=False",
            "--publish",
            f"127.0.0.1:{web_port}:8000",
            "--health-interval=1s",
            "--health-timeout=3s",
            "--health-retries=3",
            "--health-cmd",
            "python -c 'import urllib.request;urllib.request.urlopen(\"http://127.0.0.1:8000/health\",timeout=2)'",
            "--entrypoint",
            "python",
        ]
        for key, value in runtime_identity_env(expected).items():
            arguments += ["--env", key + "=" + value]
        try:
            ids.append(docker(*arguments, image, "-u", "-c", command))
            ids.append(
                docker(
                    "run",
                    "-d",
                    "--pull=never",
                    "--network",
                    "none",
                    "--name",
                    project + "-database",
                    "--env",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "postgres:17",
                )
            )
            initial_db = json.loads(docker("inspect", ids[1]))[0]
            bound_port = json.loads(docker("inspect", ids[0]))[0]["NetworkSettings"]["Ports"][
                "8000/tcp"
            ][0]["HostPort"]
            profile = store.read_product_profile_record(expected.product)
            profile.lanes[0].health_url = f"http://127.0.0.1:{bound_port}/health"
            store.write_product_profile_record(profile)
            assert (
                store.read_product_profile_record(expected.product).lanes[0].health_url
                == profile.lanes[0].health_url
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if (
                    json.loads(docker("inspect", ids[0]))[0]["State"]["Health"]["Status"]
                    == "healthy"
                ):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Isolated web did not become healthy.")

            class Provider(BaseHTTPRequestHandler):
                def log_message(self, *args: object) -> None:
                    pass

                def answer(self, value: object) -> None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(value).encode())

                def do_GET(self) -> None:
                    parsed = urlsplit(self.path)
                    query = parse_qs(parsed.query)
                    if parsed.path == "/api/compose.one":
                        assert query["composeId"] == ["isolated-compose-testing"]
                        self.answer({"composeId": "isolated-compose-testing", "appName": project})
                    elif parsed.path == "/api/docker.getContainersByAppNameMatch":
                        assert query["appName"] == [project]
                        self.answer([{"containerId": ids[0]}])
                    elif parsed.path == "/api/docker.getConfig":
                        assert query["containerId"] == [ids[0]]
                        self.answer(json.loads(docker("inspect", ids[0])))
                    else:
                        self.send_error(404)

                def do_POST(self) -> None:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    assert self.path == "/api/docker.restartContainer" and body == {
                        "containerId": ids[0]
                    }
                    writes.append(self.path)
                    docker("restart", ids[0])
                    self.answer({})

            provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
            Thread(target=provider.serve_forever, daemon=True).start()
            stack.callback(provider.server_close)
            stack.callback(provider.shutdown)
            provider_url = f"http://127.0.0.1:{provider.server_port}"
            stack.enter_context(
                patch(
                    "control_plane.dokploy.source.read_dokploy_config",
                    return_value=(provider_url, "isolated-provider-token"),
                )
            )
            app = create_launchplane_fastapi_test_app(
                local_record_store_for_tests=store,
                state_dir=root / "state",
                control_plane_root_path=root,
                verifier=StubVerifier(identity()),
                authz_policy=local_operator_policy(
                    actions=("live_target_runtime.plan", "live_target_runtime.apply")
                ),
                bearer_identity_config=BearerIdentityConfig(
                    local_operator_token="isolated-token",
                    local_operator_subject="local-owner-agent",
                    local_operator_token_label="local-owner-write",
                ),
            )
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            while not server.started:
                if not thread.is_alive():
                    raise RuntimeError("Local service failed to start.")
                time.sleep(0.1)

            def stop_service() -> None:
                server.should_exit = True
                thread.join(timeout=10)
                listener.close()

            stack.callback(stop_service)
            config = root / "operator.json"
            config.write_text(json.dumps({"service_url": f"http://127.0.0.1:{port}"}))
            env = {**os.environ, "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "isolated-token"}
            helper = str(Path(__file__).with_name("restart-lane-service.py"))
            installed = os.environ["LP_REHEARSAL_OPERATOR_HELPER"]
            common = [
                sys.executable,
                helper,
                "--operator-helper",
                installed,
                "--config",
                str(config),
                "--product",
                expected.product,
                "--context",
                expected.context,
                "--instance",
                "testing",
                "--service",
                "web",
                "--reason",
                "Isolated same-artifact restart rehearsal.",
                "--evidence-file",
                str(root / "review.json"),
            ]
            dry = json.loads(
                subprocess.check_output([*common, "dry-run"], env=env, text=True, timeout=30)
            )
            assert not writes
            apply_process = subprocess.run(
                [*common, "apply", "--reviewed-dry-run", "--idempotency-key", project],
                env=env,
                text=True,
                capture_output=True,
                timeout=130,
            )
            if apply_process.returncode:
                print(apply_process.stdout, flush=True)
                records = store.list_lane_service_restart_reservations(product=expected.product)
                print(
                    json.dumps([record.response_payload for record in records], indent=2),
                    flush=True,
                )
                raise RuntimeError("Isolated restart did not verify.")
            applied = json.loads(apply_process.stdout)
            replay = json.loads(
                subprocess.check_output(
                    [*common, "apply", "--reviewed-dry-run", "--idempotency-key", project],
                    env=env,
                    text=True,
                    timeout=30,
                )
            )
            reviewed = LaneServiceRestartResponse.model_validate_json(
                (root / "review.json").read_text()
            )
            recovery = LaneServiceRestartRecovery(
                request=LaneServiceRestartRecoveryRequest(
                    product=expected.product,
                    context=expected.context,
                    instance="testing",
                    service="web",
                    reason=reviewed.result.plan.reason,
                    mode="apply",
                    reviewed_plan_sha256=reviewed.result.plan_sha256,
                ),
                idempotency_key=project,
            )
            recovery_path = root / "recovery.json"
            recovery_path.write_text(recovery.model_dump_json())
            recovery_path.chmod(0o600)
            resumed = json.loads(
                subprocess.check_output(
                    [*common[:-1], str(recovery_path), "resume"], env=env, text=True, timeout=30
                )
            )
            assert resumed["replayed"] and len(writes) == 1
            current_db = json.loads(docker("inspect", ids[1]))[0]
            assert initial_db["State"]["StartedAt"] == current_db["State"]["StartedAt"]
            assert applied["status"] == "pass" and replay["replayed"] and len(writes) == 1
            event = next(
                event
                for event in build_product_activity_read_model(
                    record_store=store, product=expected.product
                ).events
                if event.event_type == "service_restart"
            )
            assert event.status == "pass"
            print(
                json.dumps(
                    {
                        "engine": "local Docker Desktop",
                        "dry_run": dry,
                        "apply": applied,
                        "replay": replay,
                        "read_only_resume": resumed,
                        "provider_writes": writes,
                        "database_start_unchanged": True,
                        "activity_result": event.status,
                        "production_touched": False,
                    },
                    indent=2,
                )
            )
        finally:
            for container in reversed(ids):
                docker("rm", "-f", "-v", container)


if __name__ == "__main__":
    main()
