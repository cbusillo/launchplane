#!/usr/bin/env bash

# Run packaged entrypoints against disposable PostgreSQL with no external network.
set -euo pipefail
if [ "$#" -ne 2 ] || [ -z "$1" ] || [ -z "$2" ]; then
  echo "Usage: $0 EXISTING_TEST_IMAGE POSTGRES_TEST_IMAGE" >&2
  exit 2
fi
image="$1"
postgres_image="$2"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
scratch_root="${RUNNER_TEMP:-$repo_root/state}"
mkdir -p "$scratch_root"
fixture_dir="$(mktemp -d "$scratch_root/lp-service-compose.XXXXXXXX")"
project="$(basename "$fixture_dir" | tr '[:upper:].' '[:lower:]-')"
compose=(docker compose --project-name "$project" --project-directory "$fixture_dir"
  --env-file "$fixture_dir/.env" --file "$fixture_dir/compose.json")
services=(launchplane launchplane-odoo-workers launchplane-verireel-workers)
active_pid=""
run_interruptible() {
  "$@" <&0 &
  active_pid="$!"
  wait "$active_pid"
  active_pid=""
}
cancel() {
  trap '' INT TERM
  if [ -n "$active_pid" ]; then
    kill -KILL "$active_pid" 2>/dev/null || true
    wait "$active_pid" 2>/dev/null || true
  fi
  exit "$1"
}
cleanup() {
  local result="$?"
  trap - EXIT
  trap '' INT TERM
  if [ -f "$fixture_dir/compose.json" ]; then
    if [ "$result" -ne 0 ] && [ "$result" -ne 130 ] && [ "$result" -ne 143 ]; then
      "${compose[@]}" logs --tail 30 >&2 || true
    fi
    if ! "${compose[@]}" down --volumes --remove-orphans --timeout 2; then
      echo "Fixture cleanup failed for project $project; retained $fixture_dir." >&2
      exit 1
    fi
  fi
  rm -rf "$fixture_dir"
  exit "$result"
}
trap cleanup EXIT
trap 'cancel 130' INT
trap 'cancel 143' TERM

docker image inspect "$image" >/dev/null
docker pull "$postgres_image" >/dev/null
if [ -n "$(docker ps --all --quiet --filter "label=com.docker.compose.project=$project")" ]; then
  echo "Refusing to reuse an existing Compose project." >&2
  exit 1
fi
cat >"$fixture_dir/.env" <<'ENV'
LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://postgres@postgres/postgres
LAUNCHPLANE_POLICY_TOML=schema_version = 2
LAUNCHPLANE_SERVICE_AUDIENCE=qualification.invalid
LAUNCHPLANE_DEPLOYMENT_MARKER=qualification-initial
ENV
# Resolve the source commands, dependency ordering and stop windows without any
# runtime .env. Replace only image, network, state and bootstrap fixture inputs.
DOCKER_IMAGE_REFERENCE="$image" LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK=unused-fixture-network \
  docker compose --project-name "$project" --project-directory "$fixture_dir" \
    --env-file "$fixture_dir/.env" --file "$repo_root/docker-compose.yml" \
    config --no-env-resolution --format json |
  jq --arg image "$image" --arg postgres_image "$postgres_image" --arg env_file "$fixture_dir/.env" '
    {services: (.services | with_entries(select(.key == "launchplane" or
      .key == "launchplane-odoo-workers" or .key == "launchplane-verireel-workers" or
      .key == "launchplane-browser" or .key == "launchplane-merge-train-workers" or
      .key == "launchplane-privileged-operation-workers") |
      .value.image = $image | .value.pull_policy = "never" | .value.restart = "no" |
      .value.deploy.replicas = 1 |
      .value.networks = ["qualification"] |
      if .key == "launchplane-browser" then . else
        .value.env_file = [$env_file] | .value.environment.DOCKER_IMAGE_REFERENCE = $image
      end)),
      volumes: .volumes, networks: {qualification: {internal: true}}} |
    .services.postgres = {
      image: $postgres_image, pull_policy: "never", networks: ["qualification"],
      environment: {POSTGRES_HOST_AUTH_METHOD: "trust"},
      tmpfs: ["/var/lib/postgresql/data"],
      healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres"],
        interval: "1s", timeout: "5s", retries: 30}} |
    .services.launchplane.depends_on.postgres = {condition: "service_healthy"}' \
    >"$fixture_dir/compose.next.json"
mv "$fixture_dir/compose.next.json" "$fixture_dir/compose.json"
# Source volume names stay project-scoped, never attaching an installed volume.
jq --arg prefix "$project" '.volumes |= with_entries(.value = {name: ($prefix + "-" + .key)})' \
  "$fixture_dir/compose.json" >"$fixture_dir/compose.next.json"
mv "$fixture_dir/compose.next.json" "$fixture_dir/compose.json"

check_private_copy_custody() {
  local mode="$1" service copy_marker_sha256
  run_interruptible "${compose[@]}" exec -T -e QUALIFICATION_COPY_MODE="$mode" launchplane /app/.venv/bin/python - <<'PY' >"$fixture_dir/copy-marker.sha256"
import hashlib,os
from pathlib import Path
from control_plane.contracts.odoo_testing_source import NATIVE_TESTING_COPY_CACHE_DIRECTORY
from control_plane.odoo_testing_native_source import require_private_copy_mount
root=Path(os.environ['LAUNCHPLANE_STATE_DIR'])/NATIVE_TESTING_COPY_CACHE_DIRECTORY
require_private_copy_mount(root)
marker=root/'.qualification-copy-custody'
if os.environ['QUALIFICATION_COPY_MODE']=='create':
    root.mkdir(parents=True,exist_ok=True)
    marker.write_bytes(b'qualification-only-copy-custody')
print(hashlib.sha256(marker.read_bytes()).hexdigest())
PY
  copy_marker_sha256="$(cat "$fixture_dir/copy-marker.sha256")"
  for service in launchplane-odoo-workers launchplane-verireel-workers; do
    run_interruptible "${compose[@]}" exec -T -e QUALIFICATION_COPY_SERVICE="$service" \
      -e QUALIFICATION_COPY_SHA256="$copy_marker_sha256" "$service" /app/.venv/bin/python - <<'PY'
import hashlib,os
from pathlib import Path
from control_plane.contracts.odoo_testing_source import NATIVE_TESTING_COPY_CACHE_DIRECTORY
marker=Path(os.environ['LAUNCHPLANE_STATE_DIR'])/NATIVE_TESTING_COPY_CACHE_DIRECTORY/'.qualification-copy-custody'
if os.environ['QUALIFICATION_COPY_SERVICE']=='launchplane-odoo-workers':
    assert hashlib.sha256(marker.read_bytes()).hexdigest()==os.environ['QUALIFICATION_COPY_SHA256']
else:
    assert not marker.exists(), 'Unrelated worker can read the Odoo copy volume'
PY
  done
  for service in launchplane-merge-train-workers launchplane-privileged-operation-workers; do
    run_interruptible "${compose[@]}" run --rm --no-deps -T "$service" /app/.venv/bin/python - <<'PY'
import os
from pathlib import Path
from control_plane.contracts.odoo_testing_source import NATIVE_TESTING_COPY_CACHE_DIRECTORY
marker=Path(os.environ['LAUNCHPLANE_STATE_DIR'])/NATIVE_TESTING_COPY_CACHE_DIRECTORY/'.qualification-copy-custody'
assert not marker.exists(), 'Unrelated worker can read the Odoo copy volume'
PY
  done
}

check_workers() {
  local service container_id worker_ip worker_started_at
  for service in "${services[@]:1}"; do
    container_id="$("${compose[@]}" ps --quiet "$service")"
    test -n "$container_id"
    worker_ip="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$container_id")"
    worker_started_at="$(docker inspect --format '{{.State.StartedAt}}' "$container_id")"
    run_interruptible "${compose[@]}" exec -T -e QUALIFICATION_WORKER_IP="$worker_ip" \
      -e QUALIFICATION_WORKER_STARTED_AT="$worker_started_at" launchplane /app/.venv/bin/python - <<'PY'
import os
import time
from sqlalchemy import text
from control_plane.storage.postgres import PostgresRecordStore
store = PostgresRecordStore(database_url=os.environ["LAUNCHPLANE_DATABASE_URL"])
try:
    deadline = time.monotonic() + 30
    for observation in range(2):
        while True:
            with store._engine.connect() as connection:
                ready = connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                    "WHERE datname = current_database() AND client_addr = CAST(:address AS inet) "
                    "AND backend_start >= CAST(:started_at AS timestamptz))"
                ), {"address": os.environ["QUALIFICATION_WORKER_IP"],
                    "started_at": os.environ["QUALIFICATION_WORKER_STARTED_AT"]})
            if ready:
                break
            if time.monotonic() >= deadline:
                raise AssertionError("Packaged worker never connected to the fixture database")
            time.sleep(0.2)
        if observation == 0:
            time.sleep(2)
finally:
    store.close()
PY
    test "$(docker inspect --format '{{.State.Running}}' "$container_id")" = true
  done
}

check_testing_browser() {
  # Exercise the actual packaged caller services, including the event worker.
  for service in launchplane launchplane-odoo-workers; do
    run_interruptible "${compose[@]}" exec -T "$service" /app/.venv/bin/python - <<'PY'
import time
from playwright.sync_api import sync_playwright
from control_plane.workflows.verireel_testing_post_deploy import launch_testing_browser
deadline = time.monotonic() + 60
while True:
    try:
        with sync_playwright() as playwright:
            browser = launch_testing_browser(playwright.chromium)
            try:
                page = browser.new_page()
                heading = "Packaged testing qualification"
                page.set_content(f"<h1>{heading}</h1>")
                assert page.locator("h1").inner_text() == heading
            finally:
                browser.close()
        break
    except Exception:
        if time.monotonic() >= deadline:
            raise
        time.sleep(1)
PY
  done
}

run_interruptible "${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 120
check_workers
check_private_copy_custody create
check_testing_browser
before="$("${compose[@]}" ps --quiet "${services[@]}" | sort)"
run_interruptible "${compose[@]}" exec -T launchplane /app/.venv/bin/python - <<'PY'
import os
from control_plane.service_deploy_drain import prepare, record_dispatch
from control_plane.storage.postgres import PostgresRecordStore
store = PostgresRecordStore(database_url=os.environ["LAUNCHPLANE_DATABASE_URL"])
try:
    store.verify_schema()
    fence, running, dispatch = prepare(
        store, request_fingerprint="isolated-replacement", target_type="compose",
        target_id="isolated-control-plane", image_reference=os.environ["DOCKER_IMAGE_REFERENCE"],
        deployment_marker="qualification-replacement",
    )
    assert not running and dispatch
    record_dispatch(store, fence.request_fingerprint)
finally:
    store.close()
PY
printf '%s\n' 'LAUNCHPLANE_DEPLOYMENT_MARKER=qualification-replacement' >>"$fixture_dir/.env"
run_interruptible "${compose[@]}" up --detach --no-deps --no-build --pull never --force-recreate --timeout 10 \
  --wait --wait-timeout 120 "${services[@]}"
after="$("${compose[@]}" ps --quiet "${services[@]}" | sort)"
test -n "$after"
test -z "$(comm -12 <(printf '%s\n' "$before") <(printf '%s\n' "$after"))"
check_workers
check_private_copy_custody verify
check_testing_browser
run_interruptible "${compose[@]}" exec -T launchplane /app/.venv/bin/python - <<'PY'
import json
import os
import urllib.request
from control_plane.service_deploy_drain import read_status
from control_plane.storage.postgres import PostgresRecordStore
with urllib.request.urlopen("http://127.0.0.1:8080/v1/health", timeout=5) as response:
    assert json.load(response)["status"] == "ok"
store = PostgresRecordStore(database_url=os.environ["LAUNCHPLANE_DATABASE_URL"])
try:
    store.verify_schema()
    fence = read_status(store)
    assert fence["state"] == "confirmed" and not fence["admission_paused"], fence
    assert fence["deployment_marker"] == os.environ["LAUNCHPLANE_DEPLOYMENT_MARKER"], fence
    print(json.dumps({"packaged_service_compose": "passed", "schema": store.schema_revision(),
                      "replacement_confirmed": True, "external_network": False}))
finally:
    store.close()
PY
for service in "${services[@]}"; do
  container_id="$("${compose[@]}" ps --quiet "$service")"
  test -n "$container_id"
  test "$(docker inspect --format '{{.State.Running}}' "$container_id")" = true
done
