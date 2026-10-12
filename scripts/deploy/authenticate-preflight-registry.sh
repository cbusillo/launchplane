#!/usr/bin/env bash
# Use the existing job token in a disposable config, never the runner's credentials.
set -euo pipefail
: "${GITHUB_TOKEN:?existing job token required}"
# shellcheck source=scripts/deploy/preflight-resource-owner.sh
source "$(dirname "${BASH_SOURCE[0]}")/preflight-resource-owner.sh"
registry_config="$(preflight_registry_config_path)"
mkdir -m 700 -- "$registry_config"
# Populate Docker's supported auths format directly: docker login can discover
# a persistent credential helper even with a fresh configuration directory.
DOCKER_CONFIG="$registry_config" uv run --no-project python - <<'AUTH'
import base64
import json
import os
from pathlib import Path

credential = (os.environ["GITHUB_ACTOR"] + ":" + os.environ["GITHUB_TOKEN"]).encode()
config = {"auths": {"ghcr.io": {"auth": base64.b64encode(credential).decode()}}}
path = Path(os.environ["DOCKER_CONFIG"]) / "config.json"
with path.open("x", encoding="utf-8") as output:
    os.chmod(path, 0o600)
    json.dump(config, output)
AUTH
