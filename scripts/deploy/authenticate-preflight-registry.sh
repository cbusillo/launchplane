#!/usr/bin/env bash
# Use the existing job token in a disposable config, never the runner's credentials.
set -euo pipefail
: "${GITHUB_TOKEN:?existing job token required}"
# shellcheck source=scripts/deploy/preflight-resource-owner.sh
source "$(dirname "${BASH_SOURCE[0]}")/preflight-resource-owner.sh"
registry_config="$(preflight_registry_config_path)"
mkdir -m 700 -- "$registry_config"
printf '%s' "$GITHUB_TOKEN" | DOCKER_CONFIG="$registry_config" docker login ghcr.io --username "$GITHUB_ACTOR" --password-stdin
