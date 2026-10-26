#!/usr/bin/env bash
set -euo pipefail

printf 'available=false\n' >> "${GITHUB_OUTPUT}"
rm -f "${UNITTEST_TIMINGS_CACHE}"
history_directory="$(mktemp -d "${RUNNER_TEMP}/unittest-history.XXXXXX")"
trap 'rm -rf "$history_directory"' EXIT
source_run="${GITHUB_RUN_ID}"
if [ -n "${SOURCE_SHA:-}" ]; then
  if [[ ! "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "::warning::Invalid verified timing source; retaining cold-plan fallback."
    exit 0
  fi
  source_run="$(gh api "repos/${REPOSITORY}/actions/workflows/ci.yml/runs?head_sha=${SOURCE_SHA}&status=success&event=push&per_page=100" \
    --jq '[.workflow_runs[] | select(.head_sha == env.SOURCE_SHA and .event == "push" and .conclusion == "success")] | first | .id // empty')" || source_run=""
fi
if [ -z "$source_run" ] || ! gh run download "$source_run" --repo "$REPOSITORY" \
  --name unittest-timing-history --dir "$history_directory"; then
  echo "::warning::Verified unittest timing artifact unavailable; retaining cold-plan fallback."
  exit 0
fi
if ! bash scripts/validate-unittest-history.sh "${history_directory}/history.json"; then
  echo "::warning::Invalid unittest timing artifact; retaining cold-plan fallback."
  exit 0
fi
mkdir -p "$(dirname "$UNITTEST_TIMINGS_CACHE")"
cp "${history_directory}/history.json" "$UNITTEST_TIMINGS_CACHE"
printf 'available=true\n' >> "${GITHUB_OUTPUT}"
echo "Published unittest timing history from CI run ${source_run}."
