#!/usr/bin/env bash

# Exercise the checked-in replica expression with harmless, isolated containers.
# The supplied image must already exist locally; no service code or credentials run.
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "Usage: $0 EXISTING_TEST_IMAGE" >&2
  exit 2
fi
image="$1"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
worker="launchplane-ordinary-agent-workers"
scratch_root="${RUNNER_TEMP:-$repo_root/state}"
mkdir -p "$scratch_root"
fixture_dir="$(mktemp -d "$scratch_root/lp-ordinary-compose.XXXXXXXX")"
project="$(basename "$fixture_dir" | tr '[:upper:].' '[:lower:]-')"
compose=(docker compose --project-name "$project" --project-directory "$fixture_dir"
  --env-file "$fixture_dir/.env" --file "$fixture_dir/compose.json")
touch "$fixture_dir/.env"

# Remove only this invocation's project. Never prune images, caches, or other projects.
cleanup() {
  local result="$?"
  trap - EXIT
  if [ -f "$fixture_dir/compose.json" ]; then
    if ! "${compose[@]}" down --remove-orphans --timeout 5; then
      echo "Fixture cleanup failed for project $project." >&2
      exit 1
    fi
  fi
  rm -rf "$fixture_dir"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

docker image inspect "$image" >/dev/null
existing="$(docker ps --all --quiet --filter "label=com.docker.compose.project=$project")"
if [ -n "$existing" ]; then
  echo "Refusing to reuse an existing Compose project." >&2
  rm -rf "$fixture_dir"
  exit 1
fi

render_fixture() {
  local replicas="$1"
  if [ "$replicas" = absent ]; then
    : >"$fixture_dir/.env"
  else
    printf 'LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=%s\n' "$replicas" >"$fixture_dir/.env"
  fi
  # Resolve only our empty/test env, never the repo or runtime .env. Keep the
  # source dependency/replica semantics, replacing all executable/config wiring.
  env -u LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS \
    DOCKER_IMAGE_REFERENCE="$image" \
    LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK=unused-fixture-network \
    docker compose --project-name "$project" --project-directory "$fixture_dir" \
      --env-file "$fixture_dir/.env" --file "$repo_root/docker-compose.yml" \
      config --no-env-resolution --format json |
    jq --arg image "$image" --arg worker "$worker" '
      if .services[$worker] == null then error("ordinary worker is not declared") else . end |
      {services: (.services | with_entries(
        .value = {
          image: $image,
          pull_policy: "never",
          entrypoint: [],
          command: ["/bin/sleep", "180"],
          network_mode: "none",
          read_only: true,
          user: "65534:65534",
          cap_drop: ["ALL"],
          security_opt: ["no-new-privileges:true"],
          pids_limit: 8,
          mem_limit: "32m",
          stop_grace_period: "2s",
          healthcheck: {test: ["CMD", "/bin/true"], interval: "1s", timeout: "1s", retries: 5},
          profiles: (.value.profiles // []),
          depends_on: (.value.depends_on // {}),
          deploy: {replicas: (.value.deploy.replicas // 1)}
        }
      ))}' >"$fixture_dir/compose.next.json"
  mv "$fixture_dir/compose.next.json" "$fixture_dir/compose.json"
}

other_containers() {
  "${compose[@]}" ps --all --format json |
    jq -s --arg worker "$worker" '
      [ .[] | if type == "array" then .[] else . end |
        select(.Service != $worker) | {service: .Service, id: .ID, state: .State} ] |
      sort_by(.service)'
}

render_fixture absent
"${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 30
default_worker_ids="$("${compose[@]}" ps --all --quiet "$worker")"
test -z "$default_worker_ids"
before="$(other_containers)"
expected_others="$(jq --arg worker "$worker" '[.services | keys[] | select(. != $worker)] | length' "$fixture_dir/compose.json")"
test "$(jq length <<<"$before")" = "$expected_others"
jq -e 'all(.[]; .state == "running")' <<<"$before" >/dev/null

render_fixture 1
"${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 30
worker_id="$("${compose[@]}" ps --all --quiet "$worker")"
test -n "$worker_id"
test "$(wc -l <<<"$worker_id" | tr -d ' ')" = 1
test "$(docker inspect --format '{{.State.Running}}' "$worker_id")" = true
test "$(other_containers)" = "$before"

render_fixture 0
"${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 30
disabled_worker_ids="$("${compose[@]}" ps --all --quiet "$worker")"
test -z "$disabled_worker_ids"
retained_worker_ids="$(docker ps --all --quiet --filter "id=$worker_id")"
test -z "$retained_worker_ids"
test "$(other_containers)" = "$before"
printf '%s\n' '{"ordinary_worker_compose":"passed","default_absent":true,"enabled_running":true,"disabled_container_removed":true,"unrelated_fixture_containers_preserved":true}'
