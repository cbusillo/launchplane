#!/usr/bin/env bash
# Clean only the current workflow job's evidence and explicitly labelled fixture.
set -euo pipefail
owner="${LAUNCHPLANE_PREFLIGHT_OWNER:?exact rehearsal owner required}"
if [[ ! "$owner" =~ ^[a-zA-Z0-9_.:-]{1,160}$ ]]; then
  echo "Invalid rehearsal owner." >&2
  exit 1
fi
selector="launchplane.stored-record-preflight.owner=$owner"
if [[ -n "${RUNNER_TEMP:-}" ]]; then
  rm -f -- "$RUNNER_TEMP/launchplane-stored-record-snapshot.json"
fi
container_inventory="$(docker ps -aq --filter "label=$selector")"
container_ids=()
while IFS= read -r container_id; do
  [[ -z "$container_id" ]] || container_ids+=("$container_id")
done <<< "$container_inventory"
if (( ${#container_ids[@]} )); then
  docker rm -fv "${container_ids[@]}" >/dev/null
fi
network_inventory="$(docker network ls -q --filter "label=$selector")"
network_ids=()
while IFS= read -r network_id; do
  [[ -z "$network_id" ]] || network_ids+=("$network_id")
done <<< "$network_inventory"
for network_id in "${network_ids[@]}"; do
  docker network rm "$network_id" >/dev/null
done

# Qualification uses the job token in its own temporary registry config. Never
# alter the persistent runner's Docker credentials.
if [[ -n "${DOCKER_CONFIG:-}" && -n "${RUNNER_TEMP:-}" &&
      "$DOCKER_CONFIG" == "$RUNNER_TEMP"/launchplane-preflight-registry-* ]]; then
  rm -f -- "$DOCKER_CONFIG/config.json"
  if [[ -d "$DOCKER_CONFIG" ]]; then rmdir -- "$DOCKER_CONFIG"; fi
fi
