#!/usr/bin/env bash
set -euo pipefail

# Checkout owns this directory. Cache archives retain a workspace-relative path,
# so restoring them cannot replace another runner's in-use home store objects.
export npm_config_store_dir="${GITHUB_WORKSPACE:?workspace required}/.ci-cache/pnpm-store"
store_path=$(pnpm --dir frontend store path --silent)
printf 'npm_config_store_dir=%s\n' "$npm_config_store_dir" >> "${GITHUB_ENV:?environment file required}"
printf 'path=%s\n' "$store_path" >> "${GITHUB_OUTPUT:?output file required}"
