#!/usr/bin/env bash
set -euo pipefail

validate_history() {
  uv run --no-project --no-config --python 3.14 python - "$1" <<'PY'
import math
from pathlib import Path
import sys
from control_plane.unittest_sharding import read_module_timings

timings = read_module_timings(Path(sys.argv[1]))
raise SystemExit(not timings or not all(math.isfinite(seconds) for seconds in timings.values()))
PY
}

if [ "${1:-}" = validate ]; then
  validate_history "$2"
  exit
fi

printf 'available=false\n' >> "${GITHUB_OUTPUT}"
rm -f "${UNITTEST_TIMINGS_CACHE}"
history_directory="$(mktemp -d "${RUNNER_TEMP}/unittest-history.XXXXXX")"
trap 'rm -rf "$history_directory"' EXIT
source_runs="${GITHUB_RUN_ID}"
if [ -n "${SOURCE_SHA:-}" ]; then
  if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "::warning::Invalid verified timing source; retaining cold-plan fallback."
    exit 0
  fi
  source_runs="$(gh api "repos/${REPOSITORY}/actions/workflows/ci.yml/runs?head_sha=${SOURCE_SHA}&status=success&event=push&per_page=100" \
    --jq '.workflow_runs[] | select(.head_sha == env.SOURCE_SHA and .event == "push" and .conclusion == "success") | .id')" || source_runs=""
fi
for source_run in $source_runs; do
  source_directory="${history_directory}/${source_run}"
  if ! gh run download "$source_run" --repo "$REPOSITORY" \
    --name unittest-timing-history --dir "$source_directory"; then
    continue
  fi
  if ! validate_history "${source_directory}/history.json"; then
    continue
  fi
  mkdir -p "$(dirname "$UNITTEST_TIMINGS_CACHE")"
  cp "${source_directory}/history.json" "$UNITTEST_TIMINGS_CACHE"
  printf 'available=true\n' >> "${GITHUB_OUTPUT}"
  echo "Published unittest timing history from CI run ${source_run}."
  exit 0
done
echo "::warning::Verified unittest timing history unavailable; retaining cold-plan fallback."
