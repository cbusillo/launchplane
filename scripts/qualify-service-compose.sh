#!/usr/bin/env bash

# Run packaged entrypoints against disposable PostgreSQL with no external network.
set -euo pipefail
if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "Usage: $0 EXISTING_TEST_IMAGE" >&2
  exit 2
fi
image="$1"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
scratch_root="${RUNNER_TEMP:-$repo_root/state}"
mkdir -p "$scratch_root"
fixture_dir="$(mktemp -d "$scratch_root/lp-service-compose.XXXXXXXX")"
project="$(basename "$fixture_dir" | tr '[:upper:].' '[:lower:]-')"
compose=(docker compose --project-name "$project" --project-directory "$fixture_dir"
  --env-file "$fixture_dir/.env" --file "$fixture_dir/compose.json")
services=(launchplane launchplane-odoo-workers launchplane-verireel-workers)
cleanup() {
  local result="$?"
  trap - EXIT
  if [ -f "$fixture_dir/compose.json" ]; then
    if [ "$result" -ne 0 ]; then "${compose[@]}" logs --tail 30 >&2 || true; fi
    if ! "${compose[@]}" down --volumes --remove-orphans --timeout 10; then
      echo "Fixture cleanup failed for project $project; retained $fixture_dir." >&2
      exit 1
    fi
  fi
  rm -rf "$fixture_dir"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

docker image inspect "$image" >/dev/null
docker pull postgres:18 >/dev/null
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
  jq --arg image "$image" --arg env_file "$fixture_dir/.env" '
    {services: (.services | with_entries(select(.key == "launchplane" or
      .key == "launchplane-odoo-workers" or .key == "launchplane-verireel-workers") |
      .value.image = $image | .value.pull_policy = "never" | .value.restart = "no" |
      .value.env_file = [$env_file] | .value.networks = ["qualification"] |
      .value.environment.DOCKER_IMAGE_REFERENCE = $image)),
      volumes: .volumes, networks: {qualification: {internal: true}}} |
    .services.postgres = {
      image: "postgres:18", pull_policy: "never", networks: ["qualification"],
      environment: {POSTGRES_HOST_AUTH_METHOD: "trust"},
      tmpfs: ["/var/lib/postgresql"],
      healthcheck: {test: ["CMD-SHELL", "pg_isready -U postgres"],
        interval: "1s", timeout: "5s", retries: 30}} |
    .services.launchplane.depends_on.postgres = {condition: "service_healthy"}' \
    >"$fixture_dir/compose.json"
# Source volume names stay project-scoped, never attaching an installed volume.
jq --arg prefix "$project" '.volumes |= with_entries(.value = {name: ($prefix + "-" + .key)})' \
  "$fixture_dir/compose.json" >"$fixture_dir/compose.next.json"
mv "$fixture_dir/compose.next.json" "$fixture_dir/compose.json"

"${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 120
before="$("${compose[@]}" ps --quiet "${services[@]}" | sort)"
"${compose[@]}" exec -T launchplane /app/.venv/bin/python - <<'PY'
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
"${compose[@]}" up --detach --no-deps --no-build --pull never --force-recreate \
  --wait --wait-timeout 120 "${services[@]}"
after="$("${compose[@]}" ps --quiet "${services[@]}" | sort)"
test -n "$after"
test "$before" != "$after"
"${compose[@]}" exec -T launchplane /app/.venv/bin/python - <<'PY'
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
  test "$(docker inspect --format '{{.RestartCount}}' "$container_id")" = 0
done
