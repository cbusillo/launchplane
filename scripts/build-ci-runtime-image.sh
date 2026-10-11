#!/usr/bin/env bash
set -euo pipefail

: "${LAUNCHPLANE_CI_BUILDER:?missing CI builder}"
: "${LAUNCHPLANE_CI_IMAGE_TAG:?missing CI image tag}"

build_log=$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/launchplane-image-build.XXXXXX")
trap 'rm -f "$build_log"' EXIT

build=(docker buildx build --builder "$LAUNCHPLANE_CI_BUILDER"
  --load --tag "$LAUNCHPLANE_CI_IMAGE_TAG" --pull --progress plain)

# The job-specific builder does not import optional shared cache manifests.
if "${build[@]}" --no-cache-filter runtime . 2>&1 | tee "$build_log"; then
  exit 0
else
  pipeline_status=("${PIPESTATUS[@]}")
  build_status=${pipeline_status[0]}
  if [ "${pipeline_status[1]}" -ne 0 ]; then
    exit "${pipeline_status[1]}"
  fi
fi

# Retry only the observed cache-key/layer-fetch error, never an ordinary build failure.
if ! grep -Eq 'failed to compute cache key:.*httpReadSeeker: failed open: could not fetch content descriptor sha256:[[:xdigit:]]+.*application/vnd\.(oci\.image\.layer|docker\.image\.rootfs\.diff)' "$build_log"; then
  exit "$build_status"
fi

echo '::warning::Missing build layer while computing a cache key; retrying once without build cache.'
"${build[@]}" --no-cache .
