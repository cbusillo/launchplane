#!/usr/bin/env bash
set -euo pipefail
# Keep the registry context inside this step; Buildx post-job cleanup needs
# the runner's original context, never a deleted temporary directory.
# shellcheck source=scripts/deploy/preflight-resource-owner.sh
source "$(dirname "${BASH_SOURCE[0]}")/preflight-resource-owner.sh"
DOCKER_CONFIG="$(preflight_registry_config_path)"
export DOCKER_CONFIG
timeout --kill-after=15s 240s bash <<'PREFLIGHT' | tee "$RUNNER_TEMP/launchplane-stored-record-preflight-result.json"
set -euo pipefail
postgres_image="$(uv run --extra dev python - <<'PY'
from pathlib import Path
import yaml
workflow = yaml.safe_load(Path('.github/workflows/ci.yml').read_text())
print(workflow['env']['LAUNCHPLANE_CI_POSTGRES_IMAGE'])
PY
)"
bash scripts/deploy/stored-record-preflight.sh \
  "${CANDIDATE_IMAGE:?candidate image required}" "$RUNNER_TEMP/launchplane-stored-record-snapshot.json" \
  "$postgres_image"
PREFLIGHT
