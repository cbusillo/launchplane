import base64
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.request import ProxyHandler, build_opener

import uvicorn

from control_plane.database_maintenance_host import RecoveryRequired
from control_plane.drivers import native_routes
from control_plane.service_auth import (
    GitHubOidcVerifier,
    load_authz_policy,
    parse_authz_policy_toml,
)
from control_plane.service_bootstrap import create_launchplane_service_application
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_migration import schema_migration_action
from sqlalchemy import text


HEALTH_WAIT_SECONDS = 30


def verify_service_health(store: PostgresRecordStore) -> None:
    with store._engine.connect() as connection:
        revision = str(connection.scalar(text("SELECT version_num FROM alembic_version")))
        if schema_migration_action(current_revision=revision) not in {
            "current",
            "compatible_ahead",
        }:
            raise RecoveryRequired("Normal service startup would require another migration.")
    store.seed_guest_capture_bindings()
    if policy := os.environ.get("LAUNCHPLANE_POLICY_TOML", "").strip():
        bootstrap_policy = parse_authz_policy_toml(policy)
    elif policy := os.environ.get("LAUNCHPLANE_POLICY_B64", "").strip():
        bootstrap_policy = parse_authz_policy_toml(base64.b64decode(policy).decode())
    else:
        bootstrap_policy = load_authz_policy(Path(os.environ["LAUNCHPLANE_POLICY_FILE"]))
    native_routes._validate_native_descriptor_driver_routes()
    app = create_launchplane_service_application(
        state_dir=Path("/tmp/launchplane-maintenance"),
        bootstrap_authz_policy=bootstrap_policy,
        verifier=GitHubOidcVerifier(
            audience=os.environ.get("LAUNCHPLANE_SERVICE_AUDIENCE", "localhost")
        ),
        service_record_store=store,
        start_schedulers=False,
    )
    native_routes._validate_native_fastapi_driver_routes(app)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app, log_config=None, access_log=False, lifespan="on", timeout_graceful_shutdown=5
            )
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + HEALTH_WAIT_SECONDS
            while not server.started:
                if not thread.is_alive():
                    raise RecoveryRequired("The actual API lifespan failed startup.")
                if time.monotonic() >= deadline:
                    raise TimeoutError("The actual API lifespan did not finish in time.")
                time.sleep(0.05)
            opener = build_opener(ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{port}/v1/health", timeout=5) as response:
                payload = json.load(response)
                if response.status != 200 or payload.get("status") != "ok":
                    raise RecoveryRequired("The matching-image API health response failed.")
        finally:
            server.should_exit = True
            thread.join(HEALTH_WAIT_SECONDS)
            if thread.is_alive():
                raise TimeoutError("The verification API did not finish shutdown.")
