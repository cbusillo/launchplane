#!/usr/bin/env bash
# Run the complete smoke with Vite and Chromium in one private namespace.
set -euo pipefail

: "${BROWSER_SMOKE_IMAGE:?Set the reviewed browser image from the CI workflow}"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
node_executable="$(realpath -- "$(command -v node)")"
host_network_namespace="$(readlink /proc/self/ns/net)"
container_id=""
cleanup() {
  if [[ -n "$container_id" ]]; then
    docker rm --force "$container_id" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Use this job's actual identity so the next non-container job can clean its
# workspace. No Docker socket, host network or host browser cache is mounted.
container_id="$(docker create --init --shm-size=1g \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$repo_root,target=/work" \
  --mount "type=bind,source=$node_executable,target=/usr/local/bin/node,readonly" \
  --workdir /work \
  --env CI=true --env HOME=/tmp/browser-home \
  --env PATH=/usr/local/bin:/usr/bin:/bin \
  --env "BROWSER_SMOKE_HOST_NETNS=$host_network_namespace" \
  "$BROWSER_SMOKE_IMAGE" bash -euc '
    mkdir -p "$HOME"
    namespace="$(readlink /proc/self/ns/net)"
    if [[ "$namespace" == "$BROWSER_SMOKE_HOST_NETNS" ]]; then
      echo "Browser smoke must not share the host network namespace" >&2
      exit 1
    fi
    printf "Browser namespace: %s; host: %s; UID:GID: %s:%s\n" \
      "$namespace" "$BROWSER_SMOKE_HOST_NETNS" "$(id -u)" "$(id -g)"
    node --version
    cd frontend
    exec corepack pnpm test:browser
  ')"
docker start --attach "$container_id"
exit "$(docker inspect --format '{{.State.ExitCode}}' "$container_id")"
