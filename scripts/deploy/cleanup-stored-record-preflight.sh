#!/usr/bin/env bash
# Clean only the current workflow job's evidence and explicitly labelled fixture.
set -euo pipefail
# shellcheck source=scripts/deploy/preflight-resource-owner.sh
source "$(dirname "${BASH_SOURCE[0]}")/preflight-resource-owner.sh"
selector="$(preflight_owner_label "${LAUNCHPLANE_PREFLIGHT_OWNER:?exact rehearsal owner required}")"
# Use exactly the rehearsal's daemon context, without changing later job steps.
registry_config="$(preflight_registry_config_path)"
DOCKER_CONFIG="$registry_config"
export DOCKER_CONFIG
# A daemon failure must not skip credential cleanup. Refuse unexpected directory
# contents rather than recursively deleting an unexamined file.
rm -f -- "$registry_config/config.json"
cleanup_registry() {
  if [[ -d "$registry_config" ]]; then rmdir -- "$registry_config"; fi
}
trap cleanup_registry EXIT
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

