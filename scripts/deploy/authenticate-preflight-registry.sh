#!/usr/bin/env bash
# Use the existing job token in a disposable config, never the runner's credentials.
set -euo pipefail
: "${GITHUB_TOKEN:?existing job token required}"
registry_config="$(mktemp -d "$RUNNER_TEMP/launchplane-preflight-registry-XXXXXX")"
printf 'DOCKER_CONFIG=%s\n' "$registry_config" >> "$GITHUB_ENV"
printf '%s' "$GITHUB_TOKEN" | DOCKER_CONFIG="$registry_config" docker login ghcr.io --username "$GITHUB_ACTOR" --password-stdin
