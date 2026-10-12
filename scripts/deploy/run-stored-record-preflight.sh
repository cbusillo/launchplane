#!/usr/bin/env bash
set -euo pipefail
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
